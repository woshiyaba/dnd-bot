"""桌面跑团背包、救助与多人行动边界的离线回归。"""

import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.character.inventory import HEALING_POTION, transfer_item
from src.combat.action_registry import combat_action_entries, world_action_entries
from src.combat.interrupts import build_combat_view
from src.model.combatant import PlayerCharacter
from src.model.effects import InventoryItem
from src.model.enums import LifeState
from src.services.room_service import GameRoom, RoomMember
from src.services.session_service import SessionService
from src.session.engine import SessionEngine
from src.story.loader import get_registry


def _hero(actor_id):
    return PlayerCharacter(
        id=actor_id,
        name=actor_id,
        current_hp=10,
        max_hp=20,
        controller=actor_id,
        inventory=[InventoryItem("item_healing_potion", 2)],
    )


class TabletopPlayTests(unittest.IsolatedAsyncioTestCase):
    async def _start(self):
        engine = SessionEngine()
        party = [_hero("hero"), _hero("friend")]
        with (
            patch(
                "src.dm.world_bridge.decide_turn",
                new=AsyncMock(
                    return_value={
                        "intent": "reply",
                        "reply_brief": "测试开场",
                        "world_writes": {},
                    }
                ),
            ),
            patch(
                "src.dm.world_bridge.narrate_turn_final",
                new=AsyncMock(return_value="测试营地。"),
            ),
        ):
            await engine.start_session(
                "TABLE01",
                {
                    "scene": {"location": "营地"},
                    "active_actor_id": "hero",
                    "active_user_id": "hero",
                    "user_id": "hero",
                    "party": [
                        {
                            "type": "player",
                            "controller": actor.id,
                            "card": actor.to_card(),
                        }
                        for actor in party
                    ],
                },
                opening="检查补给",
            )
        return engine

    async def test_potion_interrupt_spends_once_and_heals_downed_ally(self):
        engine = await self._start()
        state = await engine.current_state("TABLE01")
        state["party"]["friend"].take_damage(99)
        await engine.update_state(
            "TABLE01",
            {
                "party": state["party"],
                "last_check": {"success": True, "dc": 10},
            },
        )
        agent = SimpleNamespace(
            ainvoke=AsyncMock(
                return_value={
                    "messages": [
                        SimpleNamespace(
                            content=json.dumps(
                                {
                                    "schema_version": 2,
                                    "definition_id": HEALING_POTION.id,
                                    "actor_id": "hero",
                                    "selected_target_ids": ["friend"],
                                    "summary": "喂治疗药水",
                                    "checks": [],
                                    "effects": [
                                        {
                                            "id": "heal_friend",
                                            "template_id": "healing",
                                            "target_id": "friend",
                                        }
                                    ],
                                }
                            )
                        )
                    ]
                }
            )
        )
        with (
            patch(
                "src.combat.action_compiler._get_agent",
                new=AsyncMock(return_value=agent),
            ),
            patch(
                "src.dm.world_bridge.narrate_turn_final",
                new=AsyncMock(return_value="队友重新站起来。"),
            ),
        ):
            pending = await engine.action_stream(
                "TABLE01",
                {
                    "action_id": HEALING_POTION.id,
                    "target_ids": ["friend"],
                    "declared_text": "给队友喂药",
                },
                user_id="hero",
                actor_id="hero",
                display_name="勇士",
            )
            self.assertEqual(pending["status"], "interrupted")
            self.assertEqual(pending["interrupt"]["interrupt_type"], "effect_roll")
            refreshed = await engine.current_payload("TABLE01")
            self.assertEqual(
                refreshed["state"]["party"]["hero"].inventory[0].quantity, 1
            )
            self.assertIsNone(refreshed["state"]["last_check"])
            resolved = await engine.submit(
                "TABLE01", {"result": 7, "source": "virtual"}
            )
        self.assertEqual(resolved["status"], "awaiting_input")
        party = resolved["state"]["party"]
        self.assertEqual(party["hero"].inventory[0].quantity, 1)
        self.assertEqual(party["friend"].current_hp, 7)
        self.assertEqual(party["friend"].life_state, LifeState.ALIVE)
        self.assertTrue(
            any(
                message["content"] == "给队友喂药"
                for message in resolved["state"]["messages"]
            )
        )
        agent.ainvoke.assert_awaited_once()

    def test_item_availability_respects_stock_range_and_actor_life(self):
        hero, friend = _hero("hero"), _hero("friend")
        hero.current_zone, friend.current_zone = "A", "C"
        friend.take_damage(99)
        party = {actor.id: actor for actor in (hero, friend)}
        world, _ = world_action_entries(
            hero, party, canon_definitions=[], flags={}, beat_id=None, location_id=None
        )
        potion = next(item for item in world if item["action_id"] == HEALING_POTION.id)
        self.assertEqual({item["id"] for item in potion["targets"]}, {"hero", "friend"})
        combat, _ = combat_action_entries(hero, party)
        potion = next(item for item in combat if item["action_id"] == HEALING_POTION.id)
        self.assertNotIn("friend", {item["id"] for item in potion["targets"]})
        hero.inventory[0].quantity = 0
        world, _ = world_action_entries(
            hero, party, canon_definitions=[], flags={}, beat_id=None, location_id=None
        )
        self.assertFalse(world[0]["enabled"])
        hero.inventory[0].quantity = 2
        hero.take_damage(99)
        world, _ = world_action_entries(
            hero, party, canon_definitions=[], flags={}, beat_id=None, location_id=None
        )
        self.assertFalse(world[0]["enabled"])

    async def test_transfer_is_serialized_and_cannot_bypass_pending_turn(self):
        engine = await self._start()
        service = SessionService()
        service._engine = engine
        service._canon_loaded = True
        members = {
            actor_id: RoomMember(
                user_id=actor_id,
                display_name=actor_id,
                character_id=actor_id,
                access_token=actor_id,
            )
            for actor_id in ("hero", "friend")
        }
        room = GameRoom(
            room_code="TABLE01", campaign_id="", status="playing", members=members
        )
        results = await asyncio.gather(
            *[
                service.transfer_inventory(
                    room,
                    members["hero"],
                    item_id="item_healing_potion",
                    target_id="friend",
                    quantity=2,
                )
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        self.assertEqual(
            sum(isinstance(result, HTTPException) for result in results), 1
        )
        state = await engine.current_state("TABLE01")
        self.assertEqual(state["party"]["hero"].inventory[0].quantity, 0)
        self.assertEqual(state["party"]["friend"].inventory[0].quantity, 4)
        before = deepcopy(state["party"])
        for status in ("interrupted", "finished"):
            with patch.object(
                engine,
                "current_payload",
                new=AsyncMock(return_value={"status": status, "state": state}),
            ):
                with self.assertRaises(HTTPException):
                    await service.transfer_inventory(
                        room,
                        members["friend"],
                        item_id="item_healing_potion",
                        target_id="hero",
                        quantity=1,
                    )
        self.assertEqual(state["party"], before)
        state["party"]["hero"].pending_ability_points = 2
        with self.assertRaises(HTTPException):
            service._require_world_turn(
                {"status": "awaiting_input", "state": state}, members["friend"]
            )

    def test_transfer_validation_preserves_both_inventories(self):
        hero, friend = _hero("hero"), _hero("friend")
        for quantity in (-1, 0, True, 3):
            with self.assertRaises(ValueError):
                transfer_item(hero, friend, "item_healing_potion", quantity)
        with self.assertRaises(ValueError):
            transfer_item(hero, hero, "item_healing_potion", 1)
        self.assertEqual(
            [hero.inventory[0].quantity, friend.inventory[0].quantity], [2, 2]
        )

    def test_public_view_uses_live_inventory_and_only_visible_scene(self):
        get_registry().load_all()
        hero = _hero("hero")
        fighter = deepcopy(hero)
        fighter.inventory[0].quantity = 1
        member = RoomMember(
            user_id="hero",
            display_name="勇士",
            character_id="hero",
            access_token="private",
        )
        room = GameRoom(
            room_code="TABLE01",
            campaign_id="whispers_bell_tower",
            status="playing",
            members={"hero": member},
        )
        combat = build_combat_view(
            {
                "combatants": {"hero": fighter},
                "initiative_order": ["hero"],
                "current_index": 0,
            }
        )
        view = SessionService().session_view(
            room,
            member,
            {
                "status": "interrupted",
                "state": {
                    "party": {"hero": hero},
                    "scene": {
                        "actors": [
                            {"actor_id": "npc", "name": "路人", "secret": "幕后真相"}
                        ]
                    },
                },
                "interrupt": {
                    "interrupt_type": "declare_action",
                    "directed_to": {"user_id": "hero"},
                    "extra": {"combat": combat},
                },
            },
        )
        self.assertEqual(view.party[0].inventory[0]["quantity"], 1)
        self.assertEqual(view.party[0].inventory[0]["name"], "治疗药水")
        self.assertEqual(view.scene.current_actor_id, "hero")
        self.assertEqual(view.room.campaign_title, "钟楼下的低语")
        self.assertNotIn("幕后真相", view.model_dump_json())
        self.assertEqual(view.available_actions, [])


if __name__ == "__main__":
    unittest.main()
