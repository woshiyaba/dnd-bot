"""通用推进回归：仅替换模型边界，不依赖任何内置剧本。"""

import unittest
from unittest.mock import AsyncMock, patch

from src.dm import world_bridge
from src.model.canon import Canon, beat_brief
from src.model.combatant import PlayerCharacter
from src.model.dm_state import init_story
from src.services.room_service import GameRoom, RoomMember
from src.services.session_service import session_service
from src.session import story_nodes
from src.session.dm_subgraph import resolve_check
from src.session.engine import SessionEngine


def scenario(prefix="harbor", semantic=False):
    """用不同地点和 ID 验证同一协议，不读取或修改剧本文件。"""
    gate, destination = f"{prefix}_gate", f"{prefix}_inside"
    condition = {
        "id": "access",
        "kind": "semantic" if semantic else "combat_outcome",
        "predicate": (
            {"prompt": "已查明入口位置"}
            if semantic
            else {"encounter_id": "obstacle", "outcome": "players_win"}
        ),
        "description": "解决入口障碍",
    }
    canon = Canon.from_dict(
        {
            "campaign_id": prefix,
            "title": "测试旅程",
            "start_beat_id": "arrival",
            "runtime_location_scoping": True,
            "declared_flags": ["won_battle"],
            "locations": [
                {
                    "id": gate,
                    "name": f"{prefix}入口",
                    "description": "尚未发现的幕后秘密",
                },
                {
                    "id": destination,
                    "name": f"{prefix}内庭",
                    "description": "安静的庭院",
                },
            ],
            "beats": [
                {
                    "id": "arrival",
                    "title": "入口",
                    "kind": "exploration",
                    "objective": "取得通行许可",
                    "location_ids": [gate],
                    "entry_state": {"location_id": gate, "exits": [destination]},
                    "encounter": {
                        "id": "obstacle",
                        "location_id": gate,
                        "monster_ids": ["watcher"],
                        "on_win_flags": ["won_battle"],
                    },
                    "advance_conditions": [condition],
                    "exits": [{"trigger_id": "access", "next_beat_id": "inside"}],
                    "key_info": [
                        {
                            "id": "note",
                            "text": "信件证明通道通向内庭",
                            "location_id": gate,
                        }
                    ],
                },
                {
                    "id": "inside",
                    "title": "内庭",
                    "kind": "exploration",
                    "location_ids": [destination],
                    "entry_state": {"location_id": destination},
                },
                {
                    "id": "ending",
                    "title": "结束",
                    "kind": "ending",
                    "ending_outcome": "win",
                    "entry_state": {"preserve_current_scene": True},
                },
            ],
        }
    )
    story, scene = init_story(canon)
    hero = PlayerCharacter(
        id="hero", name="旅人", controller="user", current_hp=10, max_hp=10, charisma=10
    )
    state = {
        "campaign_id": prefix,
        "story": story,
        "scene": scene,
        "party": {hero.id: hero},
        "active_actor_id": hero.id,
        "messages": [],
        "campaign_log": [],
        "completed_trigger_ids": [],
        "movement_requested": False,
    }
    return canon, state


class GameplayProgressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_noncombat_success_persists_then_player_moves(self):
        for prefix in ("harbor", "orbital_station"):
            with self.subTest(prefix=prefix):
                canon, state = scenario(prefix)
                resolution = {
                    "encounter_id": "obstacle",
                    "method": "persuasion",
                    "reason": "守卫核实通行凭据后允许进入",
                }
                effects = world_bridge._normalize_check_effects(
                    {
                        "on_success": {
                            "resolved_encounter": resolution,
                            "reply_brief": resolution["reason"],
                        },
                        "on_failure": {"reply_brief": "凭据未获认可"},
                    },
                    state["scene"],
                    ["hero"],
                    beat_brief(canon, state["story"]),
                )
                state.update(
                    pending_check={"actor_id": "hero", "ability": "charisma", "dc": 12},
                    pending_effects=effects,
                    last_check={"_raw_roll": 18},
                )
                state.update(resolve_check(state))
                self.assertEqual(state["reply_brief"], resolution["reason"])
                self.assertIn("DC 12 · 成功", state["messages"][-1]["content"])
                with patch.object(story_nodes, "current_canon", return_value=canon):
                    state.update(await story_nodes.evaluate_advancement(state))
                    self.assertEqual(state["next_story"], "stay")
                    self.assertEqual(state["story"]["ready_next_beat_id"], "inside")
                    self.assertEqual(state["scene"]["location_id"], f"{prefix}_gate")
                    self.assertFalse(state["story"]["flags"].get("won_battle"))
                    self.assertIsNone(state["last_combat"])
                    self.assertEqual(state["party"]["hero"].current_hp, 10)
                    self.assertEqual(state["party"]["hero"].inventory, [])
                    state.update(await story_nodes.evaluate_advancement(state))
                    self.assertEqual(state["next_story"], "stay")
                    context = beat_brief(canon, state["story"])
                    self.assertEqual(
                        context["reachable_transitions"][0]["trigger_kind"], "action"
                    )
                    state["world_writes"] = world_bridge._world_writes(
                        {"transition_to_beat_id": "inside"}, context
                    )
                    state.update(await story_nodes.evaluate_advancement(state))
                    state.update(await story_nodes.enter_beat(state))
                    self.assertEqual(state["scene"]["location_id"], f"{prefix}_inside")
                    self.assertIsNone(state["story"]["ready_next_beat_id"])
                    self.assertIn("obstacle", state["story"]["resolved_encounters"])

    async def test_failed_check_does_not_unlock_or_move(self):
        canon, state = scenario()
        state.update(
            pending_check={"actor_id": "hero", "ability": "charisma", "dc": 12},
            last_check={"_raw_roll": 2},
            pending_effects={
                "on_success": {
                    "world_writes": {
                        "resolved_encounter": {
                            "encounter_id": "obstacle",
                            "method": "stealth",
                            "reason": "避开巡逻",
                        }
                    }
                },
                "on_failure": {
                    "completed_trigger_ids": [],
                    "reply_brief": "仍需寻找通路",
                },
            },
        )
        state.update(resolve_check(state))
        with patch.object(story_nodes, "current_canon", return_value=canon):
            result = await story_nodes.evaluate_advancement(state)
        self.assertEqual(result["next_story"], "stay")
        self.assertNotIn("ready_next_beat_id", result["story"])
        self.assertFalse(result["story"].get("resolved_encounters"))
        self.assertEqual(state["reply_brief"], "仍需寻找通路")

    async def test_semantic_decision_reused_without_second_model_call(self):
        canon, state = scenario(semantic=True)
        with (
            patch.object(story_nodes, "current_canon", return_value=canon),
            patch.object(world_bridge, "judge_trigger", new=AsyncMock()) as judge,
        ):
            result = await story_nodes.evaluate_advancement(state)
            self.assertEqual(result["story_transition"]["type"], "stay")
            state["completed_trigger_ids"] = ["access"]
            result = await story_nodes.evaluate_advancement(state)
            self.assertEqual(result["story_transition"]["type"], "ready")
            state["movement_requested"] = True
            result = await story_nodes.evaluate_advancement(state)
            self.assertEqual(result["next_story"], "advance")
            judge.assert_not_awaited()

    async def test_discovered_text_and_party_reach_final_narration(self):
        canon, state = scenario()
        state["world_writes"] = {"discoveries": ["note"]}
        with (
            patch.object(story_nodes, "current_canon", return_value=canon),
            patch.object(
                world_bridge,
                "narrate_turn_final",
                new=AsyncMock(return_value="已读到信件正文"),
            ) as narrate,
        ):
            state.update(await story_nodes.evaluate_advancement(state))
            await story_nodes.final_narrate_turn(state)
        event = next(
            event
            for event in narrate.call_args.kwargs["resolved_events"]
            if event["event"] == "clue_discovered"
        )
        self.assertEqual(event["text"], canon.beat("arrival").key_info[0].text)
        self.assertEqual(narrate.call_args.kwargs["party"]["hero"].current_hp, 10)

    async def test_invalid_resolution_and_premature_effect_are_rejected(self):
        canon, state = scenario()
        context = beat_brief(canon, state["story"])
        for resolution in (
            {"encounter_id": "future", "method": "stealth", "reason": "绕过"},
            {"encounter_id": "obstacle", "method": "stealth", "reason": ""},
        ):
            with self.assertRaises(ValueError):
                world_bridge._world_writes({"resolved_encounter": resolution}, context)
        with self.assertRaises(ValueError):
            world_bridge._normalize_decision(
                {
                    "intent": "player_check",
                    "resolved_encounter": {
                        "encounter_id": "obstacle",
                        "method": "stealth",
                        "reason": "绕过",
                    },
                },
                state["scene"],
                ["hero"],
                decision_context=context,
            )
        with self.assertRaises(ValueError):
            world_bridge._completed_trigger_ids(
                {"completed_trigger_ids": ["future"]}, context
            )

    async def test_public_view_hides_canon_private_description(self):
        canon, state = scenario()
        member = RoomMember(
            user_id="user",
            display_name="旅人",
            character_id="hero",
            access_token="test",
        )
        room = GameRoom(
            room_code="TEST",
            campaign_id=canon.campaign_id,
            status="playing",
            members={"user": member},
        )
        with patch("src.services.session_service.get_registry") as registry:
            registry.return_value.get.return_value = canon
            view = session_service.session_view(
                room, member, {"state": state, "status": "awaiting_input"}
            )
        self.assertEqual(view.scene.description, "")
        self.assertIsNone(view.scene.threat)
        self.assertEqual(view.scene.exits, ["harbor内庭"])

    async def test_full_graph_check_keeps_success_and_open_exit(self):
        canon, state = scenario()
        decisions = [
            {"intent": "reply", "reply_brief": "入口有人值守"},
            {
                "intent": "player_check",
                "check": {
                    "actor_id": "hero",
                    "ability": "charisma",
                    "dc": 12,
                    "prompt": "请掷说服检定",
                },
                "effects": {
                    "on_success": {
                        "resolved_encounter": {
                            "encounter_id": "obstacle",
                            "method": "persuasion",
                            "reason": "守卫核实凭据后放行",
                        },
                        "reply_brief": "守卫放行",
                    },
                    "on_failure": {"reply_brief": "守卫未同意"},
                },
            },
            {
                "intent": "reply",
                "reply_brief": "前往内庭",
                "movement_requested": True,
                "transition_to_beat_id": "inside",
            },
        ]
        with (
            patch("src.story.loader.CanonRegistry.get", return_value=canon),
            patch.object(
                world_bridge, "_decide_llm", new=AsyncMock(side_effect=decisions)
            ),
            patch.object(
                world_bridge,
                "narrate_turn_final",
                new=AsyncMock(return_value="本回合结束"),
            ),
        ):
            engine = SessionEngine()
            await engine.start_session(
                "PROGRESSION",
                {
                    "campaign_id": canon.campaign_id,
                    "active_actor_id": "hero",
                    "party": [
                        {
                            "type": "player",
                            "controller": "user",
                            "card": state["party"]["hero"].to_card(),
                        }
                    ],
                },
            )
            pending = await engine.message(
                "PROGRESSION", "我出示凭据说服守卫", actor_id="hero", user_id="user"
            )
            self.assertEqual(pending["status"], "interrupted")
            result = await engine.submit("PROGRESSION", 18)
            self.assertEqual(result["state"]["story"]["ready_next_beat_id"], "inside")
            result = await engine.message(
                "PROGRESSION", "我现在进入内庭", actor_id="hero", user_id="user"
            )
            self.assertEqual(result["state"]["story"]["current_beat_id"], "inside")


if __name__ == "__main__":
    unittest.main()
