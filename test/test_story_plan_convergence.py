"""StoryPlan 归一化与有界修复状态机的离线测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.schemas.story import StoryPlan
from src.story.generator import (
    StoryGenerationError,
    _generate_story_plan,
    generate_staged_canon,
)
from src.story.plan_normalizer import normalize_story_plan_candidate
from src.story.plan_repair import merge_story_plan_sections
from src.story.prompt import build_story_plan_replan_prompt
from src.story.validation import validate_story_plan_issues
from test.test_story_generation_pipeline import _brief, _standard_plan


def _plan_with_extra_beat() -> dict:
    raw = _standard_plan().model_dump()
    raw["beats"].append(
        {
            "id": "beat_extra",
            "act_id": "act_investigation",
            "kind": "exploration",
            "estimated_minutes": 4,
            "objective": "检查无关的侧廊",
            "pressure": "月蚀逼近",
            "location_ids": ["location_archive"],
            "exits": [
                {
                    "to_beat_id": "beat_climax",
                    "condition_summary": "离开侧廊",
                    "consequence": "回到仪式主线",
                }
            ],
        }
    )
    return raw


class StoryPlanNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scale_profile_is_overwritten_without_ai_repair(self):
        raw = _standard_plan().model_dump()
        raw["scale_profile"]["locations"] = 4
        completion = AsyncMock(return_value=raw)

        with patch("src.story.generator._complete_json", completion):
            plan, repair_count = await _generate_story_plan(_brief(), [])

        self.assertEqual(plan.scale_profile, _brief().scale_profile)
        self.assertEqual(repair_count, 0)
        completion.assert_awaited_once()

    def test_act_membership_and_minutes_are_rebuilt_from_beats(self):
        raw = _standard_plan().model_dump()
        raw["acts"][1]["beat_ids"] = ["beat_opening"]
        raw["acts"][1]["estimated_minutes"] = 1

        normalized = normalize_story_plan_candidate(raw, _brief())

        self.assertEqual(
            normalized["acts"][1]["beat_ids"],
            ["beat_rooftops", "beat_archive", "beat_convergence"],
        )
        self.assertEqual(normalized["acts"][1]["estimated_minutes"], 34)

    def test_branch_choices_are_rebuilt_from_source_exits(self):
        raw = _standard_plan().model_dump()
        raw["branch_points"][0]["choices"] = ["ending_win", "ending_lose"]

        normalized = normalize_story_plan_candidate(raw, _brief())

        self.assertEqual(
            normalized["branch_points"][0]["choices"],
            ["beat_rooftops", "beat_archive"],
        )

    def test_payoff_flags_are_rebuilt_from_payoff_ledger(self):
        raw = _standard_plan().model_dump()
        raw["beats"][0]["payoff_flag_ids"] = ["flag_understood_ritual"]
        raw["beats"][4]["payoff_flag_ids"] = []

        normalized = normalize_story_plan_candidate(raw, _brief())
        payoff_flags = {
            beat["id"]: beat["payoff_flag_ids"] for beat in normalized["beats"]
        }

        self.assertEqual(payoff_flags["beat_opening"], [])
        self.assertEqual(payoff_flags["beat_climax"], ["flag_understood_ritual"])

    def test_all_derived_fields_may_be_omitted_from_candidate(self):
        raw = _standard_plan().model_dump()
        raw.pop("scale_profile")
        for act in raw["acts"]:
            act.pop("beat_ids")
            act.pop("estimated_minutes")
        for beat in raw["beats"]:
            beat.pop("payoff_flag_ids")
        for branch in raw["branch_points"]:
            branch.pop("choices")

        normalized = normalize_story_plan_candidate(raw, _brief())

        self.assertEqual(
            validate_story_plan_issues(StoryPlan.model_validate(normalized), _brief()),
            [],
        )


class StoryPlanRepairStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def test_local_section_merge_rejects_topology_changes(self):
        previous = _standard_plan().model_dump()
        changed_beats = _standard_plan().model_dump()["beats"]
        changed_beats[0]["exits"][0]["to_beat_id"] = "ending_win"

        with self.assertRaisesRegex(ValueError, "不得修改对象 ID"):
            merge_story_plan_sections(
                previous=previous,
                repair={
                    "repair_kind": "story_plan_sections",
                    "sections": {"beats": changed_beats},
                },
                allowed_sections={"beats"},
            )

    def test_beat_count_issue_is_structural_and_replan_allows_id_changes(self):
        normalized = normalize_story_plan_candidate(_plan_with_extra_beat(), _brief())
        issues = validate_story_plan_issues(
            StoryPlan.model_validate(normalized), _brief()
        )
        count_issue = next(
            issue for issue in issues if "playable_beats 数量" in issue.message
        )

        prompt = build_story_plan_replan_prompt(
            candidate=normalized,
            confirmed_brief=_brief(),
            issues=issues,
            reserved_campaign_ids=[],
        )

        self.assertEqual(count_issue.category, "structural")
        self.assertIn("允许重建 acts、beats、受影响实体 ID", prompt)
        self.assertNotIn("不得新增、删除或重命名已有 ID", prompt)

    async def test_structural_replan_replaces_the_complete_dependency_closure(self):
        invalid = _plan_with_extra_beat()
        replacement = _standard_plan().model_dump()
        completion = AsyncMock(side_effect=[invalid, replacement])

        with patch("src.story.generator._complete_json", completion):
            plan, repair_count = await _generate_story_plan(_brief(), [])

        self.assertEqual(repair_count, 1)
        self.assertNotIn("beat_extra", {beat.id for beat in plan.beats})
        self.assertEqual(plan.branch_points, _standard_plan().branch_points)
        self.assertEqual(plan.effect_owner_ledger, _standard_plan().effect_owner_ledger)
        self.assertEqual(
            plan.foreshadowing_payoffs, _standard_plan().foreshadowing_payoffs
        )

    async def test_repeated_issue_fingerprint_stops_after_one_local_repair(self):
        invalid = _standard_plan().model_dump()
        invalid["effect_owner_ledger"][0]["owner_id"] = "clue_missing"
        unchanged_repair = {
            "repair_kind": "story_plan_sections",
            "sections": {
                "effect_owner_ledger": invalid["effect_owner_ledger"],
            },
        }
        completion = AsyncMock(side_effect=[invalid, unchanged_repair])

        with (
            patch("src.story.generator._complete_json", completion),
            self.assertRaisesRegex(StoryGenerationError, "连续两轮未变化"),
        ):
            await _generate_story_plan(_brief(), [])

        self.assertEqual(completion.await_count, 2)

    async def test_invalid_candidate_is_never_persisted_as_plan_artifact(self):
        invalid = {"plan_version": 1}
        completion = AsyncMock(side_effect=[invalid, invalid])
        on_artifact = AsyncMock()

        with (
            patch("src.story.generator._complete_json", completion),
            self.assertRaises(StoryGenerationError),
        ):
            await generate_staged_canon(
                confirmed_brief=_brief(),
                on_artifact=on_artifact,
            )

        on_artifact.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
