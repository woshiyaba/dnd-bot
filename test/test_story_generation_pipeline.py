"""分阶段故事生成契约、计划图与 SQLite 恢复的离线测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.common.utils.llm_util import ModelRole
from src.dm import world_bridge
from src.model.canon import Canon, beat_brief, validate_canon
from src.schemas.story import StoryDesignBrief, StoryPacing, StoryPlan
from src.services.story_service import StoryService
from src.story.generator import (
    StoryGenerationError,
    _fragment_errors,
    _load_reference_fragments,
)
from src.story.loader import get_registry
from src.story.prompt import (
    build_fragment_prompt,
    build_story_plan_prompt,
    validate_confirmed_design_brief,
)
from src.story.store import StoryGenerationStore
from src.story.validation import (
    story_plan_id_registry,
    validate_fragment_ids,
    validate_story_plan,
)
from src.session import story_nodes


def _brief(duration: int = 45) -> StoryDesignBrief:
    return StoryDesignBrief.model_validate(
        {
            "revision": 2,
            "confirmed_revision": 2,
            "premise": "在月蚀前找回被盗的星盘",
            "player_role": "受城中学者委托的一级冒险者",
            "core_conflict": "阻止盗星者完成月蚀仪式",
            "antagonist_direction": "system_design_secret",
            "gameplay_focus": ["调查", "社交", "战斗"],
            "tone": "明亮而紧张",
            "content_boundaries": [],
            "duration_minutes": duration,
            "player_count": 2,
            "ending_direction": "一个胜利结局和一个失败结局",
            "user_confirmed": True,
        }
    )


def _standard_plan() -> StoryPlan:
    raw = {
        "plan_version": 1,
        "campaign_id_candidate": "moon_astrolabe",
        "start_beat_id": "beat_opening",
        "scale_profile": {
            "playable_beats": 5,
            "acts": 3,
            "locations": 5,
            "encounters": 2,
            "clues": 4,
        },
        "acts": [
            {
                "id": "act_arrival",
                "purpose": "建立委托与第一次选择",
                "estimated_minutes": 4,
                "beat_ids": ["beat_opening"],
                "turning_point": "玩家决定先追人还是先查现场",
            },
            {
                "id": "act_investigation",
                "purpose": "两路调查汇流并升级威胁",
                "estimated_minutes": 34,
                "beat_ids": ["beat_rooftops", "beat_archive", "beat_convergence"],
                "turning_point": "确认仪式地点",
            },
            {
                "id": "act_eclipse",
                "purpose": "高潮与双结局",
                "estimated_minutes": 14,
                "beat_ids": ["beat_climax", "ending_win", "ending_lose"],
                "turning_point": "月蚀仪式被阻止或完成",
            },
        ],
        "beats": [
            {
                "id": "beat_opening",
                "act_id": "act_arrival",
                "kind": "opening",
                "estimated_minutes": 4,
                "objective": "接受委托并选择调查路线",
                "pressure": "月蚀将在一小时内开始",
                "location_ids": ["location_square"],
                "actor_ids": ["actor_scholar"],
                "clue_ids": ["clue_broken_lens"],
                "exits": [
                    {
                        "to_beat_id": "beat_rooftops",
                        "condition_summary": "追踪屋顶足迹",
                        "consequence": "先接触盗星者的守卫",
                    },
                    {
                        "to_beat_id": "beat_archive",
                        "condition_summary": "查阅旧星图",
                        "consequence": "先理解仪式结构",
                    },
                ],
            },
            {
                "id": "beat_rooftops",
                "act_id": "act_investigation",
                "kind": "exploration",
                "estimated_minutes": 14,
                "objective": "截住携带星盘零件的守卫",
                "pressure": "守卫正把零件送往仪式场",
                "location_ids": ["location_rooftops"],
                "actor_ids": ["actor_guard"],
                "clue_ids": ["clue_guard_orders"],
                "encounter_id": "encounter_guard",
                "exits": [
                    {
                        "to_beat_id": "beat_convergence",
                        "condition_summary": "取得路线信息",
                        "consequence": "带着守卫路线汇流",
                    }
                ],
            },
            {
                "id": "beat_archive",
                "act_id": "act_investigation",
                "kind": "exploration",
                "estimated_minutes": 14,
                "objective": "从旧档案找出仪式缺口",
                "pressure": "档案馆正在封闭",
                "location_ids": ["location_archive"],
                "actor_ids": ["actor_scholar"],
                "clue_ids": ["clue_eclipse_map"],
                "exits": [
                    {
                        "to_beat_id": "beat_convergence",
                        "condition_summary": "定位仪式场",
                        "consequence": "带着星图知识汇流",
                    }
                ],
            },
            {
                "id": "beat_convergence",
                "act_id": "act_investigation",
                "kind": "conflict",
                "estimated_minutes": 6,
                "objective": "穿过仪式外围",
                "pressure": "月光已经开始变暗",
                "location_ids": ["location_causeway"],
                "actor_ids": ["actor_cultist"],
                "clue_ids": ["clue_ritual_phrase"],
                "encounter_id": "encounter_cultist",
                "exits": [
                    {
                        "to_beat_id": "beat_climax",
                        "condition_summary": "突破外围",
                        "consequence": "进入仪式核心",
                    }
                ],
            },
            {
                "id": "beat_climax",
                "act_id": "act_eclipse",
                "kind": "climax",
                "estimated_minutes": 10,
                "objective": "在月蚀完成前归位星盘",
                "pressure": "仪式已进入最后阶段",
                "location_ids": ["location_observatory"],
                "actor_ids": ["actor_scholar"],
                "payoff_flag_ids": ["flag_understood_ritual"],
                "exits": [
                    {
                        "to_beat_id": "ending_win",
                        "condition_summary": "阻止仪式",
                        "consequence": "进入胜利结局",
                    }
                ],
            },
            {
                "id": "ending_win",
                "act_id": "act_eclipse",
                "kind": "ending",
                "estimated_minutes": 2,
                "objective": "回应胜利代价",
                "pressure": "无",
                "location_ids": ["location_observatory"],
            },
            {
                "id": "ending_lose",
                "act_id": "act_eclipse",
                "kind": "ending",
                "estimated_minutes": 2,
                "objective": "回应失败代价",
                "pressure": "无",
                "location_ids": ["location_observatory"],
            },
        ],
        "entities": {
            "actors": [
                {"id": "actor_scholar", "name": "学者", "summary": "委托人"},
                {"id": "actor_guard", "name": "守卫", "summary": "追兵"},
                {"id": "actor_cultist", "name": "司仪", "summary": "外围敌人"},
            ],
            "locations": [
                {"id": "location_square", "name": "广场"},
                {"id": "location_rooftops", "name": "屋顶"},
                {"id": "location_archive", "name": "档案馆"},
                {"id": "location_causeway", "name": "堤道"},
                {"id": "location_observatory", "name": "观星台"},
            ],
            "encounters": [
                {"id": "encounter_guard", "name": "屋顶阻截"},
                {"id": "encounter_cultist", "name": "堤道冲突"},
            ],
            "clues": [
                {"id": "clue_broken_lens", "name": "碎镜"},
                {"id": "clue_guard_orders", "name": "命令"},
                {"id": "clue_eclipse_map", "name": "星图"},
                {"id": "clue_ritual_phrase", "name": "仪式短句"},
            ],
            "flags": [{"id": "flag_understood_ritual", "name": "理解仪式"}],
            "items": [{"id": "item_lens_shard", "name": "镜片碎片"}],
            "actions": [],
        },
        "clue_graph": [
            {
                "clue_id": clue_id,
                "answers": "回答一个明确问题",
                "unlocks": ["下一步调查"],
                "acquisition_owner": clue_id,
                "alternative_approaches": ["观察环境", "询问在场角色"],
            }
            for clue_id in (
                "clue_broken_lens",
                "clue_guard_orders",
                "clue_eclipse_map",
                "clue_ritual_phrase",
            )
        ],
        "branch_points": [
            {
                "beat_id": "beat_opening",
                "choices": ["beat_rooftops", "beat_archive"],
                "distinct_consequences": ["获得守卫命令", "理解旧星图"],
                "reconverge_at": "beat_convergence",
            }
        ],
        "foreshadowing_payoffs": [
            {
                "flag_id": "flag_understood_ritual",
                "setup_beat_id": "beat_opening",
                "payoff_beat_id": "beat_climax",
                "description": "早先的光学异常在高潮揭示仪式弱点",
            }
        ],
        "ending_routes": [
            {"ending_id": "ending_win", "outcome": "win"},
            {"ending_id": "ending_lose", "outcome": "lose"},
        ],
        "effect_owner_ledger": [
            {
                "effect_id": "flag_understood_ritual",
                "effect_kind": "flag",
                "owner_kind": "discovery",
                "owner_id": "clue_broken_lens",
            },
            {
                "effect_id": "item_lens_shard",
                "effect_kind": "item",
                "owner_kind": "discovery",
                "owner_id": "clue_broken_lens",
            },
        ],
    }
    return StoryPlan.model_validate(raw)


class StoryDesignBriefContractTests(unittest.TestCase):
    def test_legacy_confirmed_briefs_derive_all_three_scale_profiles(self):
        expected = {
            20: ("short", 3, 0),
            45: ("standard", 5, 1),
            90: ("long", 8, 2),
        }
        for duration, (mode, beats, branches) in expected.items():
            with self.subTest(duration=duration):
                brief = _brief(duration)
                self.assertEqual(brief.length_mode, mode)
                self.assertEqual(brief.scale_profile.playable_beats, beats)
                self.assertEqual(
                    brief.branching_budget.meaningful_branch_points, branches
                )
                self.assertEqual(validate_confirmed_design_brief(brief), [])

    def test_confirmed_long_brief_reports_all_scale_conflicts_together(self):
        with self.assertRaises(ValidationError) as raised:
            StoryDesignBrief.model_validate(
                {
                    "duration_minutes": 90,
                    "length_mode": "long",
                    "scale_profile": {
                        "playable_beats": 5,
                        "acts": 3,
                        "locations": 5,
                        "encounters": 3,
                        "clues": 3,
                    },
                    "branching_style": "branch_and_reconverge",
                    "branching_budget": {
                        "meaningful_branch_points": 2,
                        "max_parallel_beats": 1,
                        "reconverge_before_climax": True,
                    },
                    "user_confirmed": True,
                }
            )

        message = str(raised.exception)
        for field in ("playable_beats", "acts", "locations", "clues"):
            self.assertIn(f"scale_profile.{field}", message)

    def test_partial_interview_can_hold_unsupported_sessions_until_confirmation(self):
        partial = StoryDesignBrief(target_sessions=2, user_confirmed=False)
        self.assertEqual(partial.target_sessions, 2)
        with self.assertRaises(ValidationError):
            StoryDesignBrief(target_sessions=2, user_confirmed=True)

    def test_pacing_must_total_one_hundred(self):
        with self.assertRaises(ValidationError):
            StoryPacing(opening_percent=11)

    def test_plan_paths_must_match_confirmed_pacing(self):
        brief = _brief().model_copy(
            update={
                "pacing": StoryPacing(
                    opening_percent=70,
                    exploration_social_percent=5,
                    escalation_percent=5,
                    climax_percent=15,
                    ending_percent=5,
                )
            }
        )
        errors = validate_story_plan(_standard_plan(), brief)
        self.assertTrue(any("节奏" in error for error in errors))


class StoryPlanValidationTests(unittest.TestCase):
    def test_valid_plan_has_dag_paths_branches_and_code_derived_ids(self):
        plan = _standard_plan()
        self.assertEqual(validate_story_plan(plan, _brief()), [])
        registry = story_plan_id_registry(plan)
        self.assertIn("trigger_beat_opening_1", registry["triggers"])
        self.assertTrue(
            {"win_condition", "lose_condition"} <= set(registry["triggers"])
        )

    def test_cycle_and_zero_beat_reconvergence_are_rejected(self):
        raw = _standard_plan().model_dump()
        rooftop = next(beat for beat in raw["beats"] if beat["id"] == "beat_rooftops")
        rooftop["exits"][0]["to_beat_id"] = "beat_opening"
        cycle_errors = validate_story_plan(StoryPlan.model_validate(raw), _brief())
        self.assertTrue(any("必须是 DAG" in error for error in cycle_errors))

        raw = _standard_plan().model_dump()
        raw["branch_points"][0]["reconverge_at"] = "beat_rooftops"
        branch_errors = validate_story_plan(StoryPlan.model_validate(raw), _brief())
        self.assertTrue(any("1–2 Beat" in error for error in branch_errors))

    def test_fragment_cannot_add_ids_or_omit_top_level_registry(self):
        plan = _standard_plan()
        registry = story_plan_id_registry(plan)
        errors = validate_fragment_ids(
            "cast", {"cast": [{"id": "unplanned_actor"}]}, registry
        )
        self.assertTrue(any("计划外" in error for error in errors))

        top = {
            "campaign_id": plan.campaign_id_candidate,
            "title": "月蚀星盘",
            "premise": "找回星盘",
            "theme": "知识",
            "tone": "紧张",
            "duration_minutes": 45,
            "length_mode": "standard",
            "act_count": 3,
            "runtime_location_scoping": True,
            "recommended_player_count": 2,
            "gameplay_focus": ["调查"],
            "content_warnings": [],
            "declared_flags": [],
            "start_beat_id": plan.start_beat_id,
            "win_condition": {
                "id": "win_condition",
                "kind": "flag",
                "predicate": {"flag": "flag_understood_ritual"},
            },
            "lose_condition": {
                "id": "lose_condition",
                "kind": "semantic",
                "predicate": {"prompt": "是否失败"},
            },
        }
        top_errors = _fragment_errors("top_level", top, plan, registry)
        self.assertTrue(
            any("精确匹配 StoryPlan flags" in error for error in top_errors)
        )

    def test_cast_fragment_requires_executable_fixed_cards(self):
        plan = _standard_plan()
        registry = story_plan_id_registry(plan)
        fragment = {
            "cast": [
                {"id": actor.id, "name": actor.name, "role": actor.summary}
                for actor in plan.entities.actors
            ]
        }
        errors = _fragment_errors("cast", fragment, plan, registry)
        self.assertEqual(
            sum("缺少固定 CombatCard" in error for error in errors),
            len(plan.entities.actors),
        )

    def test_stage_prompts_are_schema_driven_and_fragment_specific(self):
        plan = _standard_plan()
        registry = story_plan_id_registry(plan)
        planning = build_story_plan_prompt(_brief(), reserved_campaign_ids=[])
        top_level = build_fragment_prompt(
            fragment_kind="top_level",
            confirmed_brief=_brief(),
            story_plan=plan.model_dump(),
            id_registry=registry,
            effect_owner_ledger=[
                item.model_dump() for item in plan.effect_owner_ledger
            ],
        )
        cast = build_fragment_prompt(
            fragment_kind="cast",
            confirmed_brief=_brief(),
            story_plan=plan.model_dump(),
            id_registry=registry,
            effect_owner_ledger=[
                item.model_dump() for item in plan.effect_owner_ledger
            ],
        )
        self.assertIn("story_plan_json_schema", planning)
        self.assertNotIn("CombatCard", top_level)
        self.assertIn("CombatCard", cast)

    def test_reference_canons_are_reduced_to_automatic_function_fragments(self):
        fragments = _load_reference_fragments()
        covered = {
            function for fragment in fragments for function in fragment["functions"]
        }
        self.assertEqual(
            covered,
            {
                "multi_location_exploration",
                "post_combat_discovery",
                "hard_gate_and_rule_action",
                "boss_settlement",
            },
        )
        self.assertTrue(all(len(fragment["beats"]) < 5 for fragment in fragments))


class StoryGenerationStoreTests(unittest.TestCase):
    def test_restart_recovers_only_validated_artifacts_and_limits_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generation.sqlite3"
            store = StoryGenerationStore(path)
            store.create_task("task_one", {"premise": "测试"})
            self.assertEqual(store.next_queued_task()["status"], "running")
            self.assertEqual(store.begin_stage_attempt("task_one", "plan"), 1)
            store.save_artifact(
                "task_one",
                stage="planning",
                artifact_key="plan",
                payload={"validated": True},
                attempt=0,
            )
            store.begin_stage_attempt("task_one", "fragment:cast")
            store.close()

            restored = StoryGenerationStore(path)
            self.assertEqual(restored.recover_interrupted(), 1)
            self.assertEqual(
                restored.artifacts("task_one"), {"plan": {"validated": True}}
            )
            self.assertEqual(
                restored.begin_stage_attempt("task_one", "fragment:cast"), 2
            )
            with self.assertRaises(RuntimeError):
                restored.begin_stage_attempt("task_one", "fragment:cast")
            restored.close()

    def test_cancel_recovery_releases_reserved_campaign_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generation.sqlite3"
            store = StoryGenerationStore(path)
            store.create_task("task_cancel", {"premise": "测试"})
            store.next_queued_task()
            self.assertTrue(store.reserve_campaign_id("task_cancel", "reserved_story"))
            self.assertEqual(
                store.request_cancel("task_cancel")["status"], "cancel_requested"
            )
            store.close()

            restored = StoryGenerationStore(path)
            self.assertEqual(restored.recover_interrupted(), 0)
            self.assertEqual(restored.get_task("task_cancel")["status"], "cancelled")
            self.assertNotIn("reserved_story", restored.reserved_campaign_ids())
            restored.close()

    def test_reservation_is_atomic_and_completion_does_not_overwrite_draft(self):
        store = StoryGenerationStore(Path(":memory:"))
        store.create_task("task_first", {})
        store.create_task("task_second", {})
        self.assertTrue(store.reserve_campaign_id("task_first", "unique_story"))
        self.assertFalse(store.reserve_campaign_id("task_second", "unique_story"))
        store.complete_task(
            "task_first",
            draft_id="draft_first",
            campaign_id="unique_story",
            raw={"campaign_id": "unique_story"},
            quality={},
        )
        store.complete_task(
            "task_first",
            draft_id="draft_second",
            campaign_id="unique_story",
            raw={"campaign_id": "overwritten"},
            quality={},
        )
        self.assertEqual(store.get_task("task_first")["draft_id"], "draft_first")
        self.assertIsNone(store.get_draft("draft_second"))
        store.close()

    def test_expired_draft_releases_reserved_campaign_id(self):
        store = StoryGenerationStore(Path(":memory:"))
        store.create_task("task_expire", {})
        store.reserve_campaign_id("task_expire", "expiring_story")
        store.complete_task(
            "task_expire",
            draft_id="draft_expire",
            campaign_id="expiring_story",
            raw={"campaign_id": "expiring_story"},
            quality={},
        )
        with patch(
            "src.story.store.utc_now",
            return_value=datetime.now(UTC) + timedelta(minutes=31),
        ):
            self.assertIsNone(store.get_draft("draft_expire"))
        self.assertNotIn("expiring_story", store.reserved_campaign_ids())
        store.close()


class StoryGenerationServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuity_or_validation_failure_creates_no_public_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(
                Path(directory) / "canons",
                Path(directory) / "generation.sqlite3",
            )
            service._store.create_task("task_failed", _brief().model_dump())
            claimed = service._store.next_queued_task()
            with patch(
                "src.services.story_service.generate_staged_canon",
                new=AsyncMock(
                    side_effect=StoryGenerationError(
                        "定向修复后仍有秘密仪式 clue_hidden 连贯性错误"
                    )
                ),
            ):
                await service._run_task(claimed)

            response = service.get_generation_task("task_failed")
            self.assertEqual(response.status, "failed")
            self.assertIsNone(response.draft)
            self.assertNotIn("clue_hidden", response.error)
            self.assertEqual(service._store.reserved_campaign_ids(), [])
            service._store.close()

    async def test_task_response_never_exposes_persisted_plan_or_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(
                Path(directory) / "canons",
                Path(directory) / "generation.sqlite3",
            )
            service._store.create_task("task_private", _brief().model_dump())
            service._store.save_artifact(
                "task_private",
                stage="planning",
                artifact_key="plan",
                payload={"npc_secret": "月蚀谜底", "beats": ["hidden"]},
                attempt=0,
            )

            public = service.get_generation_task("task_private").model_dump_json()
            self.assertNotIn("npc_secret", public)
            self.assertNotIn("月蚀谜底", public)
            self.assertNotIn("beats", public)
            service._store.close()


class CanonRuntimeCompatibilityTests(unittest.TestCase):
    def test_new_canon_only_exposes_current_location_content(self):
        raw = json.loads(
            Path("canon/whispers_bell_tower.json").read_text(encoding="utf-8")
        )
        raw["runtime_location_scoping"] = True
        canon = Canon.from_dict(raw)
        beat = canon.beat("ruined_village")
        current_location_id = beat.entry_state["location_id"]
        brief = beat_brief(
            canon,
            {
                "current_beat_id": beat.id,
                "current_location_id": current_location_id,
                "flags": {},
                "delivered_clues": [],
                "discovered_clues": [],
            },
        )
        allowed = set(brief["allowed_discovery_clue_ids"])
        expected = {
            clue.id
            for clue in beat.key_info
            if clue.location_id in {None, current_location_id}
            and clue.id
            not in set(
                beat.encounter.on_win_discoveries if beat.encounter is not None else []
            )
        }
        self.assertEqual(allowed, expected)
        self.assertEqual(brief["reachable_encounters"], [])

    def test_old_canon_defaults_to_short_without_location_scoping(self):
        raw = json.loads(
            Path("canon/whispers_bell_tower.json").read_text(encoding="utf-8")
        )
        raw.pop("length_mode", None)
        raw.pop("act_count", None)
        raw["duration_minutes"] = 45
        canon = Canon.from_dict(raw)
        self.assertEqual(canon.length_mode, "short")
        self.assertFalse(canon.runtime_location_scoping)
        self.assertEqual(validate_canon(canon), [])


class ActRecapTests(unittest.IsolatedAsyncioTestCase):
    async def test_recap_uses_fast_story_role_and_remains_non_authoritative(self):
        narrate = AsyncMock(return_value="  冒险者带着已知事实进入下一幕。  ")
        with (
            patch("src.dm.world_bridge.dm_narrate", narrate),
            patch(
                "src.dm.world_bridge.get_model_name", return_value="provider/fast"
            ) as get_model,
        ):
            recap = await world_bridge.generate_act_recap(
                previous_recap="上一幕摘要",
                structured_events=[{"event": "clue_discovered", "clue_id": "clue_a"}],
                from_act_id="act_one",
                to_act_id="act_two",
            )

        self.assertEqual(recap, "冒险者带着已知事实进入下一幕。")
        get_model.assert_called_once_with(ModelRole.STORY_RECAP)
        prompt = narrate.await_args.args[0]
        self.assertIn("不是规则状态", prompt)
        self.assertIn("clue_discovered", prompt)
        self.assertEqual(narrate.await_args.kwargs["model_name"], "provider/fast")

    async def test_normal_and_precombat_transitions_share_cross_act_recap_helper(self):
        get_registry().load_all(Path("canon"))
        generate = AsyncMock(return_value="跨幕摘要")
        state = {
            "campaign_id": "whispers_bell_tower",
            "story": {
                "current_beat_id": "ruined_village",
                "act_recap": "旧摘要",
            },
            "campaign_log": [
                {"event": "clue_discovered", "clue_id": "clue_spirit_name"},
                {"event": "narration", "content": "不应进入结构摘要"},
            ],
        }
        with patch("src.dm.world_bridge.generate_act_recap", generate):
            normal = await story_nodes._act_recap_for_transition(
                state, "bell_tower_summit"
            )
            state["combat_request"] = {
                "before_combat": {"transition_to_beat_id": "bell_tower_summit"}
            }
            prepared = await story_nodes.prepare_engagement_act_recap(state)

        self.assertEqual(normal, "跨幕摘要")
        self.assertEqual(prepared["story"]["pending_act_recap"], "跨幕摘要")
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.await_args.kwargs["from_act_id"], "act_village_hook")
        self.assertEqual(generate.await_args.kwargs["to_act_id"], "act_bell_climax")
        self.assertEqual(
            generate.await_args.kwargs["structured_events"],
            [{"event": "clue_discovered", "clue_id": "clue_spirit_name"}],
        )


if __name__ == "__main__":
    unittest.main()
