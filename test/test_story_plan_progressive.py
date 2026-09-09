"""StoryPlan 细粒度渐进生成、固定拓扑与恢复边界测试。"""

from __future__ import annotations

import json
import re
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from src.schemas.story import (
    PlanBeatOutline,
    PlanBranchBlueprint,
    PlanComplexBatch,
    PlanEntityBudget,
    PlanEntityDraft,
    PlanFrameAct,
    StoryDesignBrief,
    StoryPlanFrame,
    StoryPlanWorkState,
)
from src.services.story_service import StoryService
from src.story.generator import (
    BranchBatch,
    StoryGenerationError,
    _batch_sizes,
    _enforce_fragment_constants,
    _finalize_progressive_plan,
    _generate_story_plan_progressively,
    _route_targets,
    _validate_branch_batch,
    _validate_entity_batch,
    _validate_entity_budget,
    generate_staged_canon,
)
from src.story.prompt import build_story_plan_stage_prompt
from src.story.validation import validate_story_plan
from test.test_story_generation_pipeline import _brief, _standard_plan
from test.test_story_plan_convergence import _plan_with_extra_beat


def _tag_json(prompt: str, tag: str) -> dict:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", prompt)
    if match is None:
        raise AssertionError(f"Prompt 缺少 {tag}")
    return json.loads(match.group(1))


def _progressive_responder(plan):
    raw = plan.model_dump()
    playable = [beat for beat in raw["beats"] if beat["kind"] != "ending"]
    beat_by_id = {beat["id"]: beat for beat in raw["beats"]}
    act_by_id = {act["id"]: act for act in raw["acts"]}
    entities = raw["entities"]
    clues = {item["clue_id"]: item for item in raw["clue_graph"]}
    endings = {item["ending_id"]: item for item in raw["ending_routes"]}

    async def respond(prompt: str, **_kwargs):
        target = _tag_json(prompt, "current_target")
        target_id = target["target_id"]
        if target_id == "frame":
            return {
                "campaign_id_candidate": raw["campaign_id_candidate"],
                "acts": [
                    {
                        "id": act["id"],
                        "purpose": act["purpose"],
                        "turning_point": act["turning_point"],
                        "playable_beat_count": sum(
                            beat["act_id"] == act["id"] for beat in playable
                        ),
                    }
                    for act in raw["acts"]
                ],
            }
        if target_id in act_by_id:
            return {
                "target_id": target_id,
                "items": [
                    {
                        key: beat[key]
                        for key in ("id", "kind", "estimated_minutes", "objective")
                    }
                    for beat in playable
                    if beat["act_id"] == target_id
                ],
            }
        if target_id == "branches":
            return {
                "target_id": target_id,
                "items": [
                    {
                        "source_beat_id": item["beat_id"],
                        "choice_beat_ids": item["choices"],
                        "reconverge_at": item["reconverge_at"],
                        "distinct_consequences": item["distinct_consequences"],
                    }
                    for item in raw["branch_points"]
                ],
            }
        if target_id.startswith("plan:beat_detail:"):
            return {
                "target_id": target_id,
                "items": [
                    {
                        "beat_id": beat_id,
                        **{
                            key: beat_by_id[beat_id][key]
                            for key in (
                                "pressure",
                                "dramatic_question",
                                "entry_hook",
                                "fail_forward",
                            )
                        },
                    }
                    for beat_id in target["beat_ids"]
                ],
            }
        if target_id == "entity_budget":
            return {
                "actors": len(entities["actors"]),
                "flags": len(entities["flags"]),
                "items": len(entities["items"]),
                "actions": len(entities["actions"]),
                "payoffs": len(raw["foreshadowing_payoffs"]),
            }
        if target_id.startswith("plan:entities:"):
            category = target["category"]
            index = int(target_id.rsplit(":", 1)[1]) - 1
            selected = entities[category][index * 5 : index * 5 + target["count"]]
            return {
                "target_id": target_id,
                "items": [
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "summary": item.get("summary") or item["name"],
                    }
                    for item in selected
                ],
            }
        if target_id.startswith("plan:placement:"):
            return {
                "target_id": target_id,
                "items": [
                    {
                        key: beat_by_id[beat_id][key]
                        for key in (
                            "location_ids",
                            "actor_ids",
                            "clue_ids",
                            "encounter_id",
                        )
                    }
                    | {"beat_id": beat_id}
                    for beat_id in target["beat_ids"]
                ],
            }
        if target_id.startswith("beat_"):
            return {
                "target_id": target_id,
                "items": [
                    {
                        "condition_summary": item["condition_summary"],
                        "consequence": item["consequence"],
                    }
                    for item in beat_by_id[target_id]["exits"]
                ],
            }
        if target_id.startswith("plan:clues:"):
            return {
                "target_id": target_id,
                "items": [
                    {
                        key: clues[clue_id][key]
                        for key in (
                            "clue_id",
                            "answers",
                            "unlocks",
                            "alternative_approaches",
                        )
                    }
                    for clue_id in target["clue_ids"]
                ],
            }
        if target_id.startswith("plan:payoffs:"):
            index = int(target_id.rsplit(":", 1)[1]) - 1
            start = index * 3
            return {
                "target_id": target_id,
                "items": raw["foreshadowing_payoffs"][start : start + target["count"]],
            }
        if target_id == "endings":
            return {
                "target_id": target_id,
                "items": [
                    {
                        "ending_id": ending_id,
                        "objective": beat_by_id[ending_id]["objective"],
                        "required_facts": endings[ending_id]["required_facts"],
                        "payoffs": endings[ending_id]["payoffs"],
                    }
                    for ending_id in ("ending_win", "ending_lose")
                ],
            }
        if target_id.startswith("plan:owners:"):
            owners = {item["effect_id"]: item for item in raw["effect_owner_ledger"]}
            return {
                "target_id": target_id,
                "items": [
                    {
                        "effect_id": target_item["effect_id"],
                        "owner_kind": owners[target_item["effect_id"]]["owner_kind"],
                        "owner_id": owners[target_item["effect_id"]]["owner_id"],
                    }
                    for target_item in target["effects"]
                ],
            }
        raise AssertionError(f"未处理 target: {target}")

    return respond


class ProgressiveSchemaTests(unittest.TestCase):
    def test_closed_schema_batch_cap_target_and_global_ids(self):
        with self.assertRaises(ValidationError):
            PlanEntityDraft.model_validate(
                {"id": "actor_a", "name": "甲", "summary": "角色", "extra": True}
            )
        with self.assertRaises(ValidationError):
            PlanComplexBatch[PlanBeatOutline].model_validate(
                {
                    "target_id": "act_one",
                    "items": [
                        {
                            "id": f"beat_{index}",
                            "kind": "exploration",
                            "estimated_minutes": 5,
                            "objective": "推进",
                        }
                        for index in range(4)
                    ],
                }
            )

        state = StoryPlanWorkState(
            frame=StoryPlanFrame(
                campaign_id_candidate="campaign_one",
                acts=[
                    PlanFrameAct(
                        id="act_one",
                        purpose="开场",
                        turning_point="转折",
                        playable_beat_count=1,
                    )
                ],
            )
        )
        duplicate = PlanComplexBatch[PlanEntityDraft](
            target_id="entities",
            items=[PlanEntityDraft(id="act_one", name="重复", summary="重复")],
        )
        self.assertTrue(_validate_entity_batch(duplicate, "wrong", 1, state))

    def test_quantity_bounds_and_batches_cover_all_length_modes(self):
        for duration in (20, 45, 90):
            brief = _brief(duration)
            playable = brief.scale_profile.playable_beats
            budget = PlanEntityBudget(
                actors=2 * playable,
                flags=playable,
                items=playable,
                actions=playable,
                payoffs=playable,
            )
            self.assertEqual(_validate_entity_budget(budget, playable), [])
            for count in (
                budget.actors,
                budget.flags,
                brief.scale_profile.locations,
                brief.scale_profile.clues,
            ):
                sizes = _batch_sizes(count, 5)
                self.assertEqual(sum(sizes), count)
                self.assertLessEqual(max(sizes), 5)


class FixedTopologyTests(unittest.TestCase):
    def test_zero_one_two_branch_topologies_are_reachable_dags(self):
        cases = ((3, []), (5, [(0, 1, 2, 3)]), (8, [(0, 1, 2, 3), (3, 4, 5, 6)]))
        for count, windows in cases:
            with self.subTest(count=count):
                outlines = [
                    PlanBeatOutline(
                        id=f"beat_{index}",
                        kind=(
                            "opening"
                            if index == 0
                            else "climax" if index == count - 1 else "exploration"
                        ),
                        estimated_minutes=5,
                        objective="推进故事",
                    )
                    for index in range(count)
                ]
                branches = [
                    PlanBranchBlueprint(
                        source_beat_id=f"beat_{source}",
                        choice_beat_ids=[f"beat_{left}", f"beat_{right}"],
                        reconverge_at=f"beat_{reconverge}",
                        distinct_consequences=["后果甲", "后果乙"],
                    )
                    for source, left, right, reconverge in windows
                ]
                self.assertEqual(
                    _validate_branch_batch(
                        BranchBatch(target_id="branches", items=branches),
                        outlines,
                        len(branches),
                    ),
                    [],
                )
                graph = _route_targets(outlines, branches)
                visited: set[str] = set()
                stack = [outlines[0].id]
                while stack:
                    current = stack.pop()
                    if current in visited or current == "ending_win":
                        continue
                    visited.add(current)
                    stack.extend(graph[current])
                self.assertEqual(visited, {item.id for item in outlines})
                positions = {item.id: index for index, item in enumerate(outlines)}
                self.assertTrue(
                    all(
                        target == "ending_win" or positions[target] > positions[source]
                        for source, targets in graph.items()
                        for target in targets
                    )
                )


class FragmentConstantEnforcementTests(unittest.TestCase):
    def test_act_fragment_mechanical_fields_are_rebuilt_from_plan(self):
        plan = _standard_plan()
        fragment = {
            "beats": [
                {
                    "id": "beat_rooftops",
                    "act_id": "act_wrong",
                    "kind": "climax",
                    "estimated_minutes": 99,
                    "location_ids": ["location_wrong"],
                    "objective": "截住携带星盘零件的守卫",
                    "pressure": "守卫正把零件送往仪式场",
                    "exits": [
                        {
                            "trigger_id": "trigger_wrong",
                            "next_beat_id": "beat_wrong",
                        }
                    ],
                    "advance_conditions": [
                        {
                            "id": "trigger_wrong",
                            "kind": "action",
                            "predicate": {"action": "取得路线信息"},
                        }
                    ],
                    "key_info": [],
                }
            ]
        }

        normalized = _enforce_fragment_constants(
            "act:act_investigation", fragment, plan
        )

        beat = normalized["beats"][0]
        self.assertEqual(beat["act_id"], "act_investigation")
        self.assertEqual(beat["kind"], "exploration")
        self.assertEqual(beat["estimated_minutes"], 14)
        self.assertEqual(beat["location_ids"], ["location_rooftops"])
        self.assertEqual(
            beat["exits"],
            [
                {
                    "trigger_id": "trigger_beat_rooftops_1",
                    "next_beat_id": "beat_convergence",
                }
            ],
        )
        self.assertEqual(beat["advance_conditions"][0]["id"], "trigger_beat_rooftops_1")
        # 模型创作的 trigger 语义保留，只有 id 被强制。
        self.assertEqual(beat["advance_conditions"][0]["kind"], "action")

    def test_top_level_fragment_derives_registry_locked_fields(self):
        plan = _standard_plan()
        fragment = {
            "campaign_id": "campaign_wrong",
            "title": "月蚀星盘",
            "premise": "找回星盘",
            "theme": "知识",
            "tone": "紧张",
            "duration_minutes": 45,
            "length_mode": "standard",
            "act_count": 3,
            "runtime_location_scoping": False,
            "recommended_player_count": 2,
            "gameplay_focus": ["调查"],
            "content_warnings": [],
            "declared_flags": [],
            "start_beat_id": "beat_wrong",
            "win_condition": {"id": "win_condition", "kind": "flag", "predicate": {}},
            "lose_condition": {
                "id": "lose_condition",
                "kind": "semantic",
                "predicate": {},
            },
        }

        normalized = _enforce_fragment_constants("top_level", fragment, plan)

        self.assertEqual(normalized["campaign_id"], "moon_astrolabe")
        self.assertEqual(normalized["start_beat_id"], "beat_opening")
        self.assertIs(normalized["runtime_location_scoping"], True)
        self.assertEqual(normalized["declared_flags"], ["flag_understood_ritual"])


class ProgressiveGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_stage_persists_and_resume_skips_completed_calls(self):
        brief = _brief()
        artifacts: dict[str, dict] = {}
        persisted = AsyncMock()
        completion = AsyncMock(side_effect=_progressive_responder(_standard_plan()))
        with patch("src.story.generator._complete_json", completion):
            plan, repairs = await _generate_story_plan_progressively(
                brief,
                [],
                artifacts=artifacts,
                on_artifact=persisted,
                on_stage_start=AsyncMock(),
            )

        self.assertEqual(repairs, 0)
        self.assertEqual(validate_story_plan(plan, brief), [])
        for key in (
            "plan:frame",
            "plan:branches",
            "plan:entity_budget",
            "plan:endings",
        ):
            self.assertIn(key, artifacts)
        prompts = [call.args[0] for call in completion.await_args_list]
        owner_prompt = next(prompt for prompt in prompts if "plan:owners:" in prompt)
        self.assertIn("月蚀将在一小时内开始", owner_prompt)
        self.assertIn("早先的光学异常在高潮揭示仪式弱点", owner_prompt)

        resumed_completion = AsyncMock()
        with patch("src.story.generator._complete_json", resumed_completion):
            resumed, resumed_repairs = await _generate_story_plan_progressively(
                brief,
                [plan.campaign_id_candidate],
                artifacts=artifacts,
                on_artifact=AsyncMock(),
                on_stage_start=AsyncMock(),
            )
        self.assertEqual(resumed, plan)
        self.assertEqual(resumed_repairs, 0)
        resumed_completion.assert_not_awaited()

    async def test_small_stage_and_final_validation_each_repair_only_once(self):
        completion = AsyncMock(side_effect=[{"bad": True}, {"still_bad": True}])
        with (
            patch("src.story.generator._complete_json", completion),
            self.assertRaises(StoryGenerationError),
        ):
            await _generate_story_plan_progressively(
                _brief(),
                [],
                artifacts={},
                on_artifact=AsyncMock(),
                on_stage_start=AsyncMock(),
            )
        self.assertEqual(completion.await_count, 2)

        final_completion = AsyncMock(return_value=_plan_with_extra_beat())
        with (
            patch("src.story.generator._complete_json", final_completion),
            self.assertRaises(StoryGenerationError),
        ):
            await _finalize_progressive_plan(_plan_with_extra_beat(), _brief(), [])
        final_completion.assert_awaited_once()

    async def test_legacy_complete_plan_skips_new_planner(self):
        planner = AsyncMock(side_effect=AssertionError("不应重新规划"))
        stage_start = AsyncMock(side_effect=StoryGenerationError("停止在 Canon 分片"))
        with (
            patch("src.story.generator._generate_compact_story_plan", planner),
            patch("src.story.generator._load_reference_fragments", return_value=[]),
            self.assertRaisesRegex(StoryGenerationError, "停止在 Canon 分片"),
        ):
            await generate_staged_canon(
                confirmed_brief=_brief(),
                resume_artifacts={"plan": _standard_plan().model_dump()},
                on_stage_start=stage_start,
            )
        planner.assert_not_awaited()
        self.assertEqual(stage_start.await_args_list[0].args[0], "fragment:top_level")

    async def test_final_plan_artifact_does_not_repeat_small_stage_repairs(self):
        plan = _standard_plan()
        persisted = AsyncMock()
        stage_start = AsyncMock(side_effect=StoryGenerationError("停止在 Canon 分片"))
        with (
            patch(
                "src.story.generator._generate_compact_story_plan",
                new=AsyncMock(return_value=(plan, 0)),
            ),
            patch("src.story.generator._load_reference_fragments", return_value=[]),
            self.assertRaisesRegex(StoryGenerationError, "停止在 Canon 分片"),
        ):
            await generate_staged_canon(
                confirmed_brief=_brief(),
                on_artifact=persisted,
                on_stage_start=stage_start,
            )
        plan_save = next(
            call for call in persisted.await_args_list if call.args[1] == "plan"
        )
        self.assertEqual(plan_save.args[0], "planning")
        self.assertEqual(plan_save.args[3], 0)

    def test_branch_budget_is_rejected_at_submission_boundary(self):
        raw = _brief().model_dump()
        raw["branching_budget"]["meaningful_branch_points"] = 2
        with self.assertRaises(ValidationError):
            StoryDesignBrief.model_validate(raw)
        with self.assertRaises(HTTPException) as raised:
            StoryService._validated_brief(raw)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("3B + 2", raised.exception.detail)

    def test_prompt_contains_full_validated_work_state(self):
        state = StoryPlanWorkState(
            frame=StoryPlanFrame(
                campaign_id_candidate="campaign_one",
                acts=[
                    PlanFrameAct(
                        id="act_one",
                        purpose="守住钟楼",
                        turning_point="钟声响起",
                        playable_beat_count=1,
                    )
                ],
            ),
            beat_outlines=[
                PlanBeatOutline(
                    id="beat_opening",
                    kind="opening",
                    estimated_minutes=10,
                    objective="找到失踪守钟人",
                )
            ],
        )
        prompt = build_story_plan_stage_prompt(
            confirmed_brief=_brief(),
            current_target={"target_id": "next"},
            response_schema=PlanEntityBudget,
            generated_story_so_far=state,
            instructions="生成数量。",
        )
        self.assertIn("守住钟楼", prompt)
        self.assertIn("找到失踪守钟人", prompt)
        self.assertNotIn("previous_prompt", prompt)


if __name__ == "__main__":
    unittest.main()
