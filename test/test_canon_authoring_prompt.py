"""玩家故事大纲编译规则的无模型测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.story.loader import load_canon_file
from src.story.prompt import (
    CANON_AUTHORING_RULE,
    STORY_INTERVIEW_RULE,
    build_canon_authoring_prompt,
    build_canon_repair_prompt,
    build_story_interview_prompt,
    validate_confirmed_design_brief,
)

_CONFIRMED_BRIEF = {
    "revision": 2,
    "confirmed_revision": 2,
    "premise": "调查一座闹鬼钟楼",
    "player_role": "受村民委托的冒险者",
    "core_conflict": "查明失踪事件并阻止怨灵",
    "antagonist_direction": "system_design_secret",
    "gameplay_focus": ["调查", "探索", "战斗"],
    "tone": "阴郁悬疑",
    "content_boundaries": [],
    "duration_minutes": 20,
    "player_count": 1,
    "ending_direction": "胜利伴随救赎，也允许失败",
    "user_confirmed": True,
}


class CanonAuthoringPromptTests(unittest.TestCase):
    """确保剧本编译规则覆盖当前引擎的重要边界。"""

    def test_rule_contains_closed_engine_contract(self):
        for required in (
            "只输出一个 JSON 对象",
            "players_win",
            "monster_ids",
            "modify_ac",
            "modify_attack_bonus",
            "add_condition",
            "零线索",
            "story_critical",
            "death_fallback",
            "recommended_player_count",
            "content_warnings",
            "唯一原子入口",
            "loot_table 只是战斗结算时展示",
            "补救路线",
        ):
            self.assertIn(required, CANON_AUTHORING_RULE)

    def test_interview_rule_requires_dialogue_and_explicit_confirmation(self):
        for required in (
            "每轮最多提出 3 个问题",
            "ready_for_confirmation",
            "玩家明确确认",
            "不能跳过用户确认",
        ):
            self.assertIn(required, STORY_INTERVIEW_RULE)

        prompt = build_story_interview_prompt(
            conversation=[{"role": "user", "content": "我想玩海上幽灵船故事"}],
            design_brief={"tone": "悬疑"},
        )
        self.assertIn("海上幽灵船故事", prompt)
        self.assertIn("previous_design_brief", prompt)

    def test_unconfirmed_brief_cannot_enter_compilation(self):
        with self.assertRaises(ValueError):
            build_canon_authoring_prompt(
                confirmed_brief={"user_confirmed": False},
            )

    def test_confirmation_must_match_current_revision(self):
        stale = {**_CONFIRMED_BRIEF, "revision": 3}
        errors = validate_confirmed_design_brief(stale)

        self.assertIn("confirmed_revision 必须等于当前 revision", errors)
        with self.assertRaises(ValueError):
            build_canon_authoring_prompt(confirmed_brief=stale)

    def test_current_canon_can_be_attached_as_structural_reference(self):
        path = Path("canon/whispers_bell_tower.json")
        raw = json.loads(path.read_text(encoding="utf-8"))
        prompt = build_canon_authoring_prompt(
            confirmed_brief=_CONFIRMED_BRIEF,
            reference_canon=raw,
        )

        self.assertIn("<confirmed_design_brief>", prompt)
        self.assertIn("<reference_canon>", prompt)
        self.assertIn('"campaign_id":"whispers_bell_tower"', prompt)
        self.assertIn("不得无故复制其剧情内容", prompt)
        self.assertEqual(
            load_canon_file(path).campaign_id,
            "whispers_bell_tower",
        )

    def test_reserved_campaign_ids_are_given_to_author(self):
        prompt = build_canon_authoring_prompt(
            confirmed_brief=_CONFIRMED_BRIEF,
            reserved_campaign_ids=["existing_story"],
        )

        self.assertIn("existing_story", prompt)
        self.assertIn("不得使用", prompt)

    def test_repair_prompt_includes_errors_and_complete_draft(self):
        prompt = build_canon_repair_prompt(
            {"campaign_id": "demo"},
            ["缺少 win_condition"],
        )

        self.assertIn("缺少 win_condition", prompt)
        self.assertIn('"campaign_id": "demo"', prompt)
        self.assertIn("完整 JSON", prompt)
        self.assertIn("唯一原子 owner", prompt)
        self.assertIn("loot_table 只作文字展示", prompt)


if __name__ == "__main__":
    unittest.main()
