"""随机种子、关键 NPC 死亡续接与受控叙事意图的无模型回归测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.combat.nodes import enter_combat
from src.dm import world_bridge
from src.model.canon import Canon, beat_brief, validate_canon
from src.model.combatant import PlayerCharacter
from src.model.dm_state import build_beat_scene, init_story
from src.session import story_nodes
from src.session.dm_subgraph import perceive
from src.session.graph import (
    _build_combat_input,
    _fold_world_casualties,
    resolve_engagement,
)
from src.story.loader import get_registry


class StoryContinuityTests(unittest.TestCase):
    """验证切拍后的战斗种子与关键 NPC 死亡都能稳定写回世界。"""

    @classmethod
    def setUpClass(cls) -> None:
        registry = get_registry()
        registry.load_all()
        cls.canon = registry.get("whispers_bell_tower")

    @staticmethod
    def _player() -> PlayerCharacter:
        """构造测试玩家角色。"""
        player = PlayerCharacter.from_card(
            {
                "id": "pc_aldous",
                "name": "奥尔德斯",
                "current_hp": 20,
                "max_hp": 20,
                "ac": 16,
            }
        )
        player.controller = "user_aldous"
        return player

    def test_ad_hoc_combat_after_transition_keeps_session_seed(self):
        """复现酒馆切拍后攻击神父的路径，战斗输入必须取得会话种子。"""
        story, scene = init_story(self.canon)
        scene["dm_mode"] = "llm"
        scene["random_seed"] = 314159
        player = self._player()
        state = {
            "campaign_id": self.canon.campaign_id,
            "story": story,
            "scene": scene,
            "party": {player.id: player},
            "campaign_log": [],
        }

        transition = story_nodes.transition_to_beat(state, "ruined_village")
        state.update(transition)
        state["combat_request"] = {
            "encounter_id": None,
            "target_actor_ids": ["priest_eda"],
            "reason": "玩家主动攻击神父艾达",
        }
        engagement = resolve_engagement(state)
        state.update(engagement)
        combatants, context = _build_combat_input(state)

        self.assertEqual(state["scene"]["random_seed"], 314159)
        self.assertEqual(context["random_seed"], 314159)
        self.assertIn("priest_eda", combatants)

    def test_none_seed_is_omitted_and_enter_combat_accepts_it(self):
        """场景与请求都无种子时应沿用当前骰源，而不是执行 int(None)。"""
        ruined = self.canon.beat("ruined_village")
        scene = build_beat_scene(self.canon, ruined)
        player = self._player()
        combatants, context = _build_combat_input(
            {
                "scene": scene,
                "party": {player.id: player},
                "combat_request": {
                    "monster_ids": ["priest_eda"],
                    "random_seed": None,
                },
            }
        )

        self.assertNotIn("random_seed", context)
        result = enter_combat({"combatants": combatants, "scene_context": context})
        self.assertEqual(len(result["combatants"]), 2)

    def test_critical_npc_death_is_persistent_and_filters_dm_context(self):
        """关键 NPC 死亡后不再作为活人在场，并向 DM 暴露受约束的续接信息。"""
        ruined = self.canon.beat("ruined_village")
        scene = build_beat_scene(self.canon, ruined)
        player = self._player()
        story = {
            "current_beat_id": ruined.id,
            "flags": {},
            "delivered_clues": [],
            "discovered_clues": [],
            "removed_actor_ids": [],
            "critical_npc_deaths": [],
            "idle_turns": 3,
        }
        state = {
            "campaign_id": self.canon.campaign_id,
            "scene": scene,
            "story": story,
            "party": {player.id: player},
        }

        updated_scene, updated_story, events = _fold_world_casualties(
            state, {"priest_eda"}
        )
        brief = beat_brief(self.canon, updated_story)
        hint = story_nodes.stuck_hint_for(
            {**state, "scene": updated_scene, "story": updated_story}
        )

        self.assertNotIn(
            "priest_eda",
            {actor.get("actor_id") for actor in updated_scene["actors"]},
        )
        self.assertEqual(updated_story["critical_npc_deaths"], ["priest_eda"])
        self.assertEqual(events[0]["event"], "critical_npc_death")
        self.assertEqual(brief["npcs"], [])
        self.assertEqual(brief["critical_npc_deaths"][0]["actor_id"], "priest_eda")
        self.assertEqual(updated_story["flags"], {})
        self.assertEqual(updated_story["discovered_clues"], [])
        self.assertEqual(player.inventory, [])
        self.assertIn("艾达已死", hint)
        self.assertNotIn("艾达拉住你", hint)

    def test_mayor_death_leaves_authored_route_into_main_story(self):
        """委托人死亡后仍应有 canon 授权的任务入口，并保留失信后果。"""
        story, scene = init_story(self.canon)
        story["idle_turns"] = 3
        player = self._player()
        state = {
            "campaign_id": self.canon.campaign_id,
            "scene": scene,
            "story": story,
            "party": {player.id: player},
        }

        updated_scene, updated_story, _ = _fold_world_casualties(
            state, {"elder_marlon"}
        )
        updated_state = {**state, "scene": updated_scene, "story": updated_story}
        brief = story_nodes.beat_brief_for(updated_state)
        hint = story_nodes.stuck_hint_for(updated_state)

        death = brief["critical_npc_deaths"][0]
        self.assertIn("废村路线图", death["guidance"])
        self.assertIn("恐惧", death["consequence"])
        self.assertIn("染血路线图", hint)
        self.assertNotIn("村长马伦再次恳求", hint)

    def test_repeated_casualty_fold_does_not_duplicate_critical_death(self):
        """恢复或重复折回同一结果时不得重复记录关键 NPC 死亡。"""
        ruined = self.canon.beat("ruined_village")
        player = self._player()
        state = {
            "campaign_id": self.canon.campaign_id,
            "scene": build_beat_scene(self.canon, ruined),
            "story": {
                "current_beat_id": ruined.id,
                "removed_actor_ids": ["priest_eda"],
                "critical_npc_deaths": ["priest_eda"],
            },
            "party": {player.id: player},
        }

        _, story, events = _fold_world_casualties(state, {"priest_eda"})

        self.assertEqual(story["critical_npc_deaths"], ["priest_eda"])
        self.assertEqual(events, [])

    def test_critical_npc_requires_authored_death_fallback(self):
        """关键 NPC 缺少保底方向时 canon 必须拒绝加载。"""
        raw = json.loads(
            Path("canon/whispers_bell_tower.json").read_text(encoding="utf-8")
        )
        raw["cast"][0].pop("death_fallback")

        errors = validate_canon(Canon.from_dict(raw))

        self.assertIn("关键 NPC «elder_marlon» 缺少 death_fallback", errors)

    def test_discovered_clue_text_survives_across_beats(self):
        """DM 画像应持续携带已发现正文，但不泄露当前拍未发现的环境线索。"""
        canon = get_registry().get("prodigal_return_quest")
        brief = beat_brief(
            canon,
            {
                "current_beat_id": "chamber_climax",
                "flags": {"key_obtained": True},
                "delivered_clues": ["clue_corpse_note"],
                "discovered_clues": ["clue_corpse_note"],
            },
        )

        self.assertEqual(brief["known_clues"][0]["id"], "clue_corpse_note")
        self.assertIn("黑风宗密信", brief["known_clues"][0]["text"])
        self.assertNotIn("clue_sigil_frost", brief["discovered_clue_ids"])


class NarrativeIntentTests(unittest.IsolatedAsyncioTestCase):
    """验证 DM 的小心思被保留，但最终提示仍受规则与 canon 约束。"""

    def test_perceive_clears_previous_narrative_intent(self):
        """每个玩家回合必须清掉上回合的隐藏叙事意图。"""
        update = perceive(
            {
                "messages": [],
                "user_input": "继续调查",
                "narrative_intent": "旧伏笔",
            }
        )

        self.assertEqual(update["narrative_intent"], "")

    async def test_final_narration_receives_intent_and_death_boundaries(self):
        """最终叙述提示应携带巧思与死亡续接，同时禁止把叙述当作世界写入。"""
        narrator = AsyncMock(return_value="钟声低低回荡。你可以检查祷文或前往枯井。")
        context = {
            "beat_id": "ruined_village",
            "npcs": [],
            "critical_npc_deaths": [
                {
                    "actor_id": "priest_eda",
                    "guidance": "通过遗留祷文继续调查",
                    "consequence": "失去直接讲解者",
                }
            ],
        }

        with patch("src.dm.world_bridge.dm_narrate", narrator):
            await world_bridge.narrate_turn_final(
                user_input="进攻黑袍神父",
                reply_brief=None,
                narrative_intent="让断裂钟绳像未说完的话一样轻摆",
                last_check=None,
                last_combat={
                    "outcome": "players_win",
                    "automatic_discoveries": [
                        {
                            "id": "clue_corpse_note",
                            "text": "黑风宗密信说明他们打算用火攻压制火蟒。",
                            "granted_items": [{"item_id": "item_copper_key"}],
                        }
                    ],
                },
                previous_scene=None,
                scene={"location": "废村·钟楼前厅", "actors": []},
                beat_brief=context,
                story_transition={"type": "stay"},
                messages=[],
                use_llm=True,
            )

        task = narrator.await_args.args[0]
        self.assertIn("断裂钟绳", task)
        self.assertIn("critical_npc_deaths", task)
        self.assertIn("死者不得重新行动或说话", task)
        self.assertIn("不能仅靠叙述自动授予线索 flag", task)
        self.assertIn("黑风宗密信", task)
        self.assertIn("item_copper_key", task)
        self.assertNotIn("最后一句必须以", task)

    def test_decision_normalizes_optional_narrative_intent(self):
        """三类 DM 决策共用的隐藏叙事意图应通过规范化边界。"""
        result = world_bridge._normalize_decision(
            {
                "intent": "reply",
                "reply_brief": "引导玩家检查钟绳。",
                "narrative_intent": "用摇晃的影子暗示钟声并未真正停止。",
            },
            {},
            [],
        )

        self.assertEqual(
            result["narrative_intent"],
            "用摇晃的影子暗示钟声并未真正停止。",
        )


if __name__ == "__main__":
    unittest.main()
