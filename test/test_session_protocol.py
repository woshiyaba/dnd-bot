"""小程序公开会话协议的无模型单元测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import HTTPException

import src.app as app_module
from src.app import _public_payload, _require_pending_interrupt, _validate_manual_resume
from src.app import SessionRollRequest, roll_session_interrupt
from src.combat.dice import roll_virtual_dice
from src.combat.interrupts import build_combat_view
from src.model.combatant import Monster, PlayerCharacter
from src.session.engine import SessionEngine


class _FakeGraph:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def aget_state(self, _config):
        return self._snapshot


class _FakeSessionEngine:
    def __init__(self, current):
        self.current = current
        self.resume_value = None

    async def current_payload(self, _room_id):
        return self.current

    async def submit_stream(self, room_id, resume_value, *, event_sink=None):
        self.resume_value = resume_value
        return {
            "status": "awaiting_input",
            "room_id": room_id,
            "say": "骰子落定。",
            "state": {
                "user_id": "user_aria",
                "messages": [{"role": "dm", "content": "骰子落定。"}],
                "scene": {},
                "story": {},
                "campaign_log": [],
            },
        }


class SessionPayloadTests(unittest.IsolatedAsyncioTestCase):
    """验证刷新负载会恢复 LangGraph 当前中断。"""

    async def test_current_payload_contains_interrupt(self):
        request = {
            "interrupt_type": "ability_check",
            "directed_to": {"combatant_id": "pc_aria", "user_id": "user_aria"},
            "required_dice": "d20",
        }
        snapshot = SimpleNamespace(
            values={
                "user_id": "user_aria",
                "messages": [],
                "story_status": "ongoing",
                "reply_brief": "只供 DM 使用的计划",
                "scene": {"location": "破钟酒馆", "random_seed": 123},
                "story": {"visited_beats": ["start"], "delivered_clues": []},
            },
            interrupts=(SimpleNamespace(value=request),),
        )
        engine = SessionEngine.__new__(SessionEngine)
        engine._graph = _FakeGraph(snapshot)

        payload = _public_payload(await engine.current_payload("room_one"))

        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["interrupt"], request)
        self.assertNotIn("__interrupt__", payload["state"])
        self.assertNotIn("reply_brief", payload["state"])
        self.assertNotIn("random_seed", payload["state"]["scene"])
        self.assertEqual(payload["state"]["story"]["visited_count"], 1)

    async def test_roll_endpoint_generates_and_submits_server_d20(self):
        current = {
            "status": "interrupted",
            "room_id": "room_one",
            "state": {"user_id": "user_aria"},
            "interrupt": {
                "interrupt_type": "ability_check",
                "directed_to": {"user_id": "user_aria"},
                "required_dice": "d20",
            },
        }
        fake_engine = _FakeSessionEngine(current)
        previous_engine = app_module._session_engine
        previous_loaded = app_module._canon_loaded
        app_module._session_engine = fake_engine
        app_module._canon_loaded = True
        app_module._session_locks.clear()
        try:
            response = await roll_session_interrupt(
                "room_one", SessionRollRequest(user_id="user_aria")
            )
        finally:
            app_module._session_engine = previous_engine
            app_module._canon_loaded = previous_loaded
            app_module._session_locks.clear()

        body = json.loads(response.body)
        self.assertEqual(body["status"], "awaiting_input")
        self.assertEqual(fake_engine.resume_value["source"], "virtual")
        self.assertGreaterEqual(fake_engine.resume_value["d20"], 1)
        self.assertLessEqual(fake_engine.resume_value["d20"], 20)


class ResumeValidationTests(unittest.TestCase):
    """验证实体骰和行动选择的 HTTP 信任边界。"""

    def test_manual_d20_is_normalized_and_marked(self):
        interrupt_request = {
            "interrupt_type": "attack_roll",
            "directed_to": {"user_id": "user_aria"},
        }
        result = _validate_manual_resume(interrupt_request, {"d20": 17})
        self.assertEqual(result, {"d20": 17, "source": "manual"})

    def test_manual_d20_out_of_range_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_manual_resume({"interrupt_type": "ability_check"}, {"d20": 21})
        self.assertEqual(raised.exception.status_code, 422)

    def test_action_must_be_present_in_options(self):
        interrupt_request = {
            "interrupt_type": "declare_action",
            "options": {
                "attack": [
                    {
                        "attack_name": "长剑",
                        "targets": [{"id": "goblin", "name": "哥布林"}],
                    }
                ]
            },
        }
        accepted = _validate_manual_resume(
            interrupt_request,
            {
                "action_type": "attack",
                "attack_name": "长剑",
                "target_id": "goblin",
            },
        )
        self.assertEqual(accepted["target_id"], "goblin")

        with self.assertRaises(HTTPException):
            _validate_manual_resume(
                interrupt_request,
                {
                    "action_type": "attack",
                    "attack_name": "长剑",
                    "target_id": "dragon",
                },
            )

    def test_only_directed_user_can_resume(self):
        payload = {
            "status": "interrupted",
            "state": {"user_id": "user_aria"},
            "interrupt": {"directed_to": {"user_id": "user_aria"}},
        }
        with self.assertRaises(HTTPException) as raised:
            _require_pending_interrupt(payload, "user_other")
        self.assertEqual(raised.exception.status_code, 403)


class VirtualDiceTests(unittest.TestCase):
    """验证服务端虚拟骰表达式和公开战况摘要。"""

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
            {"id": "pc_aria", "name": "艾莉亚", "current_hp": 20, "max_hp": 30}
        )
        enemy = Monster.from_card(
            {"id": "goblin", "name": "哥布林", "current_hp": 7, "max_hp": 7}
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
