"""多人房间公开会话协议的无模型单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import HTTPException

from src.combat.dice import roll_virtual_dice
from src.combat.interrupts import build_combat_view
from src.model.combatant import Monster, PlayerCharacter
from src.services.room_service import GameRoom, RoomMember
from src.services.session_service import session_service
from src.session.engine import SessionEngine
from src.story.loader import get_registry


class _FakeGraph:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def aget_state(self, _config):
        return self._snapshot


class _FakeSessionEngine:
    def __init__(self, current, events=None):
        self.current = current
        self.resume_value = None
        self.events = events

    async def current_payload(self, _room_id):
        return self.current

    async def submit_stream(self, room_id, resume_value, *, event_sink=None):
        if self.events is not None:
            self.events.append("submit")
        self.resume_value = resume_value
        return {
            "status": "awaiting_input",
            "room_id": room_id,
            "say": "骰子落定。",
            "state": {
                "messages": [{"role": "dm", "content": "骰子落定。"}],
                "scene": {},
                "party": {},
            },
        }


def _member(user_id: str = "user_aria") -> RoomMember:
    return RoomMember(
        user_id=user_id,
        display_name="阿丽娅",
        character_id="pc_aldous",
        access_token="token",
        is_host=True,
    )


def _room(member: RoomMember | None = None) -> GameRoom:
    player = member or _member()
    return GameRoom(
        room_code="ROOM01",
        campaign_id="whispers_bell_tower",
        status="playing",
        members={player.user_id: player},
    )


class SessionPayloadTests(unittest.IsolatedAsyncioTestCase):
    """验证刷新负载与服务器骰中断协议。"""

    async def test_current_payload_contains_interrupt(self):
        request = {
            "interrupt_type": "ability_check",
            "directed_to": {
                "combatant_id": "pc_aldous",
                "user_id": "user_aria",
            },
            "prompt": "请掷感知检定",
            "required_dice": "d20",
        }
        snapshot = SimpleNamespace(
            values={
                "messages": [],
                "story_status": "ongoing",
                "reply_brief": "只供 DM 使用的计划",
                "scene": {"location": "破钟酒馆", "random_seed": 123},
                "party": {},
            },
            interrupts=(SimpleNamespace(value=request),),
        )
        engine = SessionEngine.__new__(SessionEngine)
        engine._graph = _FakeGraph(snapshot)

        payload = await engine.current_payload("ROOM01")
        view = session_service.session_view(_room(), _member(), payload)

        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["interrupt"], request)
        self.assertTrue(view.pending_interaction.is_yours)
        self.assertEqual(view.pending_interaction.required_dice, "d20")
        self.assertFalse(hasattr(view, "state"))

    async def test_interaction_roll_generates_and_submits_server_d20(self):
        member = _member()
        room = _room(member)
        current = {
            "status": "interrupted",
            "room_id": room.room_code,
            "state": {"messages": [], "scene": {}, "party": {}},
            "interrupt": {
                "interrupt_type": "ability_check",
                "directed_to": {"user_id": member.user_id},
                "required_dice": "d20",
            },
        }
        fake_engine = _FakeSessionEngine(current)
        previous_engine = session_service._engine
        previous_loaded = session_service._canon_loaded
        session_service._engine = fake_engine
        session_service._canon_loaded = True
        try:
            payload, roll = await session_service.roll_interaction(room, member)
        finally:
            session_service._engine = previous_engine
            session_service._canon_loaded = previous_loaded
            session_service._room_locks.clear()

        self.assertEqual(payload["status"], "awaiting_input")
        self.assertEqual(fake_engine.resume_value["source"], "virtual")
        self.assertGreaterEqual(roll.total, 1)
        self.assertLessEqual(roll.total, 20)

    async def test_interaction_roll_is_broadcast_before_session_resume(self):
        member = _member()
        room = _room(member)
        current = {
            "status": "interrupted",
            "room_id": room.room_code,
            "state": {"messages": [], "scene": {}, "party": {}},
            "interrupt": {
                "interrupt_type": "ability_check",
                "directed_to": {"user_id": member.user_id},
                "required_dice": "d20",
            },
        }
        events = []
        fake_engine = _FakeSessionEngine(current, events)
        previous_engine = session_service._engine
        previous_loaded = session_service._canon_loaded

        async def record_broadcast(_room, _roll):
            events.append("broadcast")

        session_service._engine = fake_engine
        session_service._canon_loaded = True
        try:
            with patch.object(session_service, "broadcast_roll", new=record_broadcast):
                await session_service.roll_interaction(room, member)
        finally:
            session_service._engine = previous_engine
            session_service._canon_loaded = previous_loaded
            session_service._room_locks.clear()

        self.assertEqual(events, ["broadcast", "submit"])

    async def test_session_view_only_projects_discovered_clues(self):
        get_registry().load_all()
        member = _member()
        room = GameRoom(
            room_code="ROOM02",
            campaign_id="prodigal_return_quest",
            status="playing",
            members={member.user_id: member},
        )
        payload = {
            "status": "awaiting_input",
            "state": {
                "campaign_id": room.campaign_id,
                "messages": [],
                "scene": {},
                "party": {},
                "story": {
                    "current_beat_id": "gate_exploration",
                    "discovered_clues": [
                        "clue_corpse_note",
                        "clue_elder_weakness",
                    ],
                },
            },
        }

        view = session_service.session_view(room, member, payload)
        serialized = view.model_dump_json()

        self.assertEqual(
            [clue.id for clue in view.clues],
            ["clue_corpse_note", "clue_elder_weakness"],
        )
        self.assertIn("黑风宗密信", view.clues[0].text)
        self.assertNotIn("寒冰浮雕", serialized)


class ResumeValidationTests(unittest.TestCase):
    """验证行动选择和目标用户边界。"""

    def test_action_must_be_present_in_options(self):
        options = {
            "attack": [
                {
                    "attack_name": "长剑",
                    "targets": [{"id": "goblin", "name": "哥布林"}],
                }
            ]
        }
        accepted = session_service.validate_action_resume(
            options,
            {
                "action_type": "attack",
                "attack_name": "长剑",
                "target_id": "goblin",
            },
        )
        self.assertEqual(accepted["target_id"], "goblin")

        with self.assertRaises(HTTPException):
            session_service.validate_action_resume(
                options,
                {
                    "action_type": "attack",
                    "attack_name": "长剑",
                    "target_id": "dragon",
                },
            )

    def test_only_directed_user_can_resume(self):
        payload = {
            "status": "interrupted",
            "interrupt": {"directed_to": {"user_id": "user_aria"}},
        }
        with self.assertRaises(HTTPException) as raised:
            session_service.require_pending_interrupt(payload, _member("user_other"))
        self.assertEqual(raised.exception.status_code, 403)


class VirtualDiceTests(unittest.TestCase):
    """验证服务器骰表达式和公开战况摘要。"""

    def test_virtual_d20_is_in_range(self):
        for _ in range(100):
            roll = roll_virtual_dice("d20")
            self.assertGreaterEqual(roll.total, 1)
            self.assertLessEqual(roll.total, 20)

    def test_critical_damage_doubles_dice_not_modifier(self):
        roll = roll_virtual_dice("1d8+4", crit=True)
        self.assertEqual(roll.count, 2)
        self.assertEqual(len(roll.rolls), 2)
        self.assertEqual(roll.modifier, 4)
        self.assertEqual(roll.total, sum(roll.rolls) + 4)

    def test_combat_view_only_contains_public_fields(self):
        player = PlayerCharacter.from_card(
            {
                "id": "pc_aria",
                "name": "艾莉亚",
                "current_hp": 20,
                "max_hp": 30,
            }
        )
        enemy = Monster.from_card(
            {
                "id": "goblin",
                "name": "哥布林",
                "current_hp": 7,
                "max_hp": 7,
            }
        )
        view = build_combat_view(
            {
                "combatants": {player.id: player, enemy.id: enemy},
                "initiative_order": [player.id, enemy.id],
                "current_index": 0,
                "current_round": 2,
            }
        )
        self.assertEqual(view["round"], 2)
        self.assertEqual(view["current_actor_id"], player.id)
        self.assertEqual(len(view["combatants"]), 2)
        self.assertNotIn("controller", view["combatants"][0])


if __name__ == "__main__":
    unittest.main()
