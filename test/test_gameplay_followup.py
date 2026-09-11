"""证物落袋、终局收束与战斗场景的通用回归，不读取指定剧本。"""

import unittest
from unittest.mock import AsyncMock, patch

from src.character.inventory import transfer_item
from src.combat import dm_bridge, nodes
from src.dm import world_bridge
from src.model.canon import beat_brief
from src.model.combatant import PlayerCharacter
from src.model.effects import InventoryItem
from src.session import story_nodes
from src.session.dm_subgraph import resolve_check
from test.test_gameplay_progression import scenario


class GameplayFollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_evidence_survives_transfer_and_cannot_be_collected_twice(self):
        for prefix, names in (
            ("harbor", ("信件", "徽章")),
            ("orbital_station", ("芯片", "样本瓶")),
        ):
            with self.subTest(prefix=prefix):
                canon, state = scenario(prefix)
                canon.beat("arrival").key_info[
                    0
                ].text = f"{names[0]}与{names[1]}放在桌上，证明通道通向内庭。"
                collection = [{"clue_id": "note", "name": name} for name in names]
                state["world_writes"] = world_bridge._world_writes(
                    {"discoveries": ["note"], "collect_evidence": collection},
                    beat_brief(canon, state["story"]),
                )
                with patch.object(story_nodes, "current_canon", return_value=canon):
                    state.update(story_nodes.apply_world_writes(state))
                    hero = state["party"]["hero"]
                    self.assertEqual(
                        [item.name for item in hero.inventory], list(names)
                    )
                    self.assertEqual(
                        [
                            event["name"]
                            for event in state["campaign_log"]
                            if event["event"] == "item_granted"
                        ],
                        list(names),
                    )
                    friend = PlayerCharacter(
                        id="friend", name="同伴", current_hp=10, max_hp=10
                    )
                    state["party"][friend.id] = friend
                    transfer_item(hero, friend, hero.inventory[0].item_id, 1)
                    self.assertEqual(
                        InventoryItem.from_dict(friend.inventory[0].to_dict()).name,
                        names[0],
                    )
                    state["world_writes"] = {"collect_evidence": collection}
                    state.update(story_nodes.apply_world_writes(state))
                    self.assertEqual(
                        sum(
                            item.quantity
                            for actor in state["party"].values()
                            for item in actor.inventory
                        ),
                        2,
                    )
                    self.assertEqual(len(state["story"]["collected_evidence"]), 2)

    def test_evidence_requires_discovery_and_grounded_current_source(self):
        canon, state = scenario()
        context = beat_brief(canon, state["story"])
        valid = {"clue_id": "note", "name": "信件"}
        for data in (
            {"collect_evidence": [valid]},
            {
                "discoveries": ["note"],
                "collect_evidence": [{"clue_id": "note", "name": "万能神器"}],
            },
            {
                "discoveries": ["future"],
                "collect_evidence": [{"clue_id": "future", "name": "信件"}],
            },
        ):
            with self.subTest(data=data), self.assertRaises(
                world_bridge.WorldStateDecisionError
            ):
                world_bridge._world_writes(data, context)
        state["story"]["discovered_clues"] = ["note"]
        state["story"]["current_beat_id"] = "inside"
        state["story"]["current_location_id"] = "harbor_inside"
        with self.assertRaises(world_bridge.WorldStateDecisionError):
            world_bridge._world_writes(
                {"collect_evidence": [valid]}, beat_brief(canon, state["story"])
            )
        state["story"]["current_beat_id"] = "arrival"
        state["story"]["current_location_id"] = "harbor_gate"
        canon.beat("arrival").key_info[0].discovery_effects = {
            "grant_items": [{"item_id": "item_map"}]
        }
        self.assertEqual(beat_brief(canon, state["story"])["evidence_sources"], [])

    def test_failed_check_does_not_grant_evidence(self):
        canon, state = scenario()
        branch = {
            "discoveries": ["note"],
            "collect_evidence": [{"clue_id": "note", "name": "信件"}],
        }
        context = beat_brief(canon, state["story"])
        with self.assertRaises(world_bridge.WorldStateDecisionError):
            world_bridge._normalize_decision(
                {"intent": "player_check", **branch},
                state["scene"],
                ["hero"],
                decision_context=context,
            )
        state.update(
            pending_check={"actor_id": "hero", "ability": "dexterity", "dc": 15},
            pending_effects=world_bridge._normalize_check_effects(
                {"on_success": branch, "on_failure": {"reply_brief": "没能取出证物"}},
                state["scene"],
                ["hero"],
                context,
            ),
            last_check={"_raw_roll": 1},
        )
        state.update(resolve_check(state))
        with patch.object(story_nodes, "current_canon", return_value=canon):
            state.update(story_nodes.apply_world_writes(state))
        self.assertEqual(state["party"]["hero"].inventory, [])
        self.assertNotIn("note", state["story"].get("discovered_clues", []))

    async def test_ending_narration_receives_terminal_contract(self):
        canon, state = scenario()
        state["story"]["current_beat_id"] = "ending"
        with patch.object(
            world_bridge, "dm_narrate", new=AsyncMock(return_value="本次冒险结束。")
        ) as narrate:
            await world_bridge.narrate_turn_final(
                user_input="我完成了最后的目标",
                reply_brief="继续追问",
                narrative_intent=None,
                last_check=None,
                last_combat=None,
                previous_scene=None,
                scene=state["scene"],
                beat_brief=beat_brief(canon, state["story"]),
                story_transition={"type": "advance"},
                messages=[],
                use_llm=True,
                party=state["party"],
            )
        prompt = narrate.call_args.args[0]
        self.assertIn("终局状态：已结束，玩家无法继续输入", prompt)
        self.assertIn("不得让玩家继续追问", prompt)
        self.assertIn("必须忽略这些行动邀请", prompt)

    async def test_combat_narration_keeps_scene_and_recent_history(self):
        _, state = scenario()
        with patch.object(
            dm_bridge, "dm_narrate", new=AsyncMock(return_value="剑锋落空。")
        ) as narrate:
            await nodes.narrate(
                {
                    "combatants": state["party"],
                    "current_round": 3,
                    "scene_context": {"location": "轨道站控制室"},
                    "turn_events": [{"event": "miss", "actor": "hero"}],
                    "combat_log": [
                        {"event": "narration", "text": text}
                        for text in ("旧开场", "前一次交锋", "刚才的交锋")
                    ],
                }
            )
        prompt = narrate.call_args.args[0]
        self.assertIn("轨道站控制室", prompt)
        self.assertIn("刚才的交锋", prompt)
        self.assertNotIn("旧开场", prompt)
        self.assertIn("不改变地点", prompt)


if __name__ == "__main__":
    unittest.main()
