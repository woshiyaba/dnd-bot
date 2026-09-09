"""仅替换模型边界，验证紧凑生成、真实校验与持久化恢复。"""

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.schemas.story import StoryDesignBrief, StoryPlan, StoryPlanCore
from src.services.story_service import StoryService
from src.story.generator import (
    StoryGenerationError,
    _canon_fragments,
    _enforce_fragment_constants,
    _fingerprint,
    _fragment_errors,
    _full_canon_errors,
    _repair_canon_objects,
    generate_staged_canon,
)
from src.story.store import StoryGenerationStore, utc_now
from src.story.validation import story_plan_id_registry, validate_canon_playability
from test.test_story_generation_pipeline import _brief, _standard_plan


def generated_story():
    """完整标准篇测试数据，所有生产校验均保持启用。"""
    brief = _brief()
    data = _standard_plan().model_dump(exclude_none=True)
    data["plan_version"] = 2
    data["win_condition"] = {
        "id": "win_condition",
        "kind": "semantic",
        "predicate": {"prompt": "星盘归位且仪式停止"},
    }
    data["lose_condition"] = {
        "id": "lose_condition",
        "kind": "combat_outcome",
        "predicate": {"outcome": "players_lose"},
    }
    for beat in data["beats"]:
        if beat["kind"] != "ending":
            beat["fail_forward"] = "调查失败时，守卫的遗留笔记提供相同线索和下一站位置"
        for index, exit_ in enumerate(beat["exits"], start=1):
            exit_["trigger"] = {
                "id": f"trigger_{beat['id']}_{index}",
                "kind": "action",
                "predicate": {"action": exit_["condition_summary"]},
            }
    plan = StoryPlan.model_validate(data)
    core = StoryPlanCore.model_validate(
        {
            "campaign_id_candidate": plan.campaign_id_candidate,
            "acts": [
                {
                    "id": act.id,
                    "purpose": act.purpose,
                    "turning_point": act.turning_point,
                    "playable_beat_count": sum(
                        beat.kind != "ending" and beat.act_id == act.id
                        for beat in plan.beats
                    ),
                }
                for act in plan.acts
            ],
            "truth": "星盘被司仪夺走，仪式将使月光消失",
            "character_motivations": ["学者希望夺回星盘"],
            "causal_chain": ["夺走星盘导致月光异常", "发现线索后追踪仪式并归位星盘"],
            "ending_intent": "回应仪式成败",
        }
    )
    reference = json.loads(
        Path("canon/whispers_bell_tower.json").read_text(encoding="utf-8")
    )
    card = next(actor["card"] for actor in reference["cast"] if actor.get("card"))
    cast = [
        {
            "id": actor.id,
            "name": actor.name,
            "role": actor.summary or "故事角色",
            "goal": "守护观星台",
            "secret": "知道星盘的来历",
            "disposition": "neutral",
            "card": {**deepcopy(card), "id": actor.id, "name": actor.name},
        }
        for actor in plan.entities.actors
    ]
    raw = {
        "campaign_id": plan.campaign_id_candidate,
        "title": "月蚀星盘",
        "premise": brief.premise,
        "theme": "调查星盘",
        "win_condition": data["win_condition"],
        "lose_condition": data["lose_condition"],
        "cast": cast,
        "locations": [
            {
                "id": loc.id,
                "name": loc.name,
                "description": "月光照亮调查现场",
                "intra_exits": [],
            }
            for loc in plan.entities.locations
        ],
        "beats": [],
        "action_definitions": [],
    }
    raw.update(_enforce_fragment_constants("top_level", {}, plan, brief))
    for planned in plan.beats:
        beat = {
            "id": planned.id,
            "title": planned.objective,
            "objective": planned.objective,
            "pressure": planned.pressure,
            "entry_state": {
                "location_id": planned.location_ids[0],
                "actors": [
                    {
                        "actor_id": actor_id,
                        "name": next(
                            actor.name
                            for actor in plan.entities.actors
                            if actor.id == actor_id
                        ),
                        "disposition": "neutral",
                        "location_id": planned.location_ids[0],
                        "type": "npc",
                    }
                    for actor_id in planned.actor_ids
                ],
                "exits": [],
            },
            "key_info": [],
            "stuck_fallback": {"hint": planned.fail_forward or "回应结局"},
        }
        for clue_id in planned.clue_ids:
            clue = next(clue for clue in plan.clue_graph if clue.clue_id == clue_id)
            effects = {"flags_set": {}, "grant_items": []}
            for owner in plan.effect_owner_ledger:
                if owner.owner_id == clue_id:
                    if owner.effect_kind == "flag":
                        effects["flags_set"][owner.effect_id] = True
                    else:
                        effects["grant_items"].append(
                            {"item_id": owner.effect_id, "quantity": 1}
                        )
            beat["key_info"].append(
                {
                    "id": clue_id,
                    "text": clue.answers,
                    "location_id": planned.location_ids[0],
                    "discovery_hints": clue.alternative_approaches,
                    "discovery_effects": effects,
                }
            )
        if planned.encounter_id:
            beat["encounter"] = {
                "id": planned.encounter_id,
                "location_id": planned.location_ids[0],
                "monster_ids": planned.actor_ids,
            }
        if planned.kind == "ending":
            beat["ending_outcome"] = "win" if planned.id == "ending_win" else "lose"
        raw["beats"].append(beat)
    raw["beats"] = _enforce_fragment_constants(
        "endings", {"beats": raw["beats"]}, plan
    )["beats"]
    return brief, core, plan, raw


def model_responder(core, plan, raw, calls):
    fragments = _canon_fragments(raw, plan)

    async def respond(prompt, *, stage, call_context=None, **kwargs):
        if call_context:
            await call_context.reserve_call(stage)
        calls.append((stage, len(prompt)))
        if stage == "计划 故事核心与章节":
            return core.model_dump()
        if stage == "计划":
            return plan.model_dump(exclude_none=True)
        if stage.startswith("分片 "):
            return deepcopy(fragments[stage.removeprefix("分片 ")])
        if "连贯性" in stage:
            return {"passed": True, "issues": []}
        raise AssertionError(stage)

    return respond


class StoryReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_pipeline_sends_options_to_actual_http_boundary(self):
        import httpx
        from langchain_openai import ChatOpenAI
        from src.common.utils.llm_util import ModelRole
        from src.schemas.story import StoryContinuityReview
        from src.story.generator import _complete_json

        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            truncated = len(requests) == 4
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"passed":'
                                    if truncated
                                    else '{"passed":true,"issues":[]}'
                                ),
                            },
                            "finish_reason": "length" if truncated else "stop",
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(
                model="deepseek-v4-pro",
                api_key="test-only",
                base_url="https://test.invalid/v1",
                http_async_client=client,
                max_retries=0,
            )
            with (
                patch("src.story.generator.get_chat_model", return_value=model),
                patch(
                    "src.story.generator.get_model_name",
                    return_value="deepseek/deepseek-v4-pro",
                ),
            ):
                for role in (
                    ModelRole.STORY_PLANNING_FAST,
                    ModelRole.STORY_AUTHORING,
                    ModelRole.STORY_CONTINUITY,
                ):
                    await _complete_json(
                        "返回复核结果",
                        stage="测试",
                        role=role,
                        schema=StoryContinuityReview,
                    )
                await _complete_json(
                    "只输出 JSON",
                    stage="截断测试",
                    role=ModelRole.STORY_CONTINUITY,
                    schema=StoryContinuityReview,
                )
        self.assertEqual(len(requests), 5)
        self.assertTrue(
            all("JSON" in request["messages"][0]["content"] for request in requests)
        )
        self.assertTrue(all(request["max_tokens"] == 8192 for request in requests))
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertEqual(requests[1]["thinking"], {"type": "disabled"})
        self.assertEqual(requests[2]["reasoning_effort"], "low")
        self.assertEqual(requests[4]["thinking"], {"type": "disabled"})

    async def test_complete_generation_uses_real_validation_budget_and_store(self):
        brief, core, plan, raw = generated_story()
        canon, errors = _full_canon_errors(raw, brief, plan)
        self.assertEqual(errors, [])
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(
                canon_dir=Path(directory), db_path=Path(directory) / "tasks.sqlite3"
            )
            with closing(service._store):
                service._store.create_task("complete", brief.model_dump())
                with patch(
                    "src.story.generator._complete_json",
                    side_effect=model_responder(core, plan, raw, calls),
                ):
                    await service._run_task(service._store.next_queued_task())
                task = service._store.get_task("complete")
                self.assertEqual(task["status"], "completed", task["error"])
                self.assertLessEqual(len(calls), 12)
                self.assertLess(sum(size for _, size in calls), 190_000)
                self.assertEqual(task["llm_call_count"], len(calls))
                artifacts = service._store.artifacts("complete")
                completion = AsyncMock(
                    side_effect=AssertionError("已验证产物不应再次调用模型")
                )
                with patch("src.story.generator._complete_json", completion):
                    restored, _, metrics = await generate_staged_canon(
                        confirmed_brief=brief, resume_artifacts=artifacts
                    )
                self.assertEqual(restored, artifacts["assembled_canon"]["canon"])
                self.assertTrue(metrics.continuity_passed)
                await service.publish(task["draft_id"])
                self.assertTrue(
                    (Path(directory) / f"{plan.campaign_id_candidate}.json").exists()
                )
                service._store.close()

    async def test_invalid_fragment_elements_return_errors(self):
        _, _, plan, _ = generated_story()
        for fragment in ({"cast": [42]}, {"locations": [None]}, {"beats": [42]}):
            kind = {
                "cast": "cast",
                "locations": "locations",
                "beats": "act:act_arrival",
            }[next(iter(fragment))]
            self.assertTrue(
                _fragment_errors(kind, fragment, plan, story_plan_id_registry(plan))
            )

    async def test_object_repair_preserves_other_objects_and_rejects_id_changes(self):
        brief, _, plan, raw = generated_story()
        changed = {**raw["cast"][0], "secret": "星盘由司仪夺走"}
        with patch(
            "src.story.generator._complete_json",
            side_effect=[
                {f"cast:{changed['id']}": changed},
                {"objects": {f"cast:{changed['id']}": changed}},
            ],
        ) as completion:
            repaired = await _repair_canon_objects(
                raw,
                ids={changed["id"]},
                errors=["角色秘密矛盾"],
                brief=brief,
                plan=plan,
                call_context=None,
            )
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(repaired["cast"][1:], raw["cast"][1:])
        self.assertEqual(repaired["beats"], raw["beats"])
        changed["id"] = "actor_changed"
        with (
            patch(
                "src.story.generator._complete_json",
                return_value={"objects": {"cast:actor_scholar": changed}},
            ),
            self.assertRaises(StoryGenerationError),
        ):
            await _repair_canon_objects(
                raw,
                ids={"actor_scholar"},
                errors=["角色秘密矛盾"],
                brief=brief,
                plan=plan,
                call_context=None,
            )

    async def test_global_failure_payoff_is_not_ordered_before_setup(self):
        from src.story.generator import _validate_story_plan_candidate

        brief, _, plan, _ = generated_story()
        raw = plan.model_dump()
        raw["foreshadowing_payoffs"][0]["payoff_beat_id"] = "ending_lose"
        _, issues, _ = _validate_story_plan_candidate(raw, brief)
        self.assertEqual(issues, [])

    async def test_branch_resources_are_not_merged(self):
        brief, _, plan, raw = generated_story()
        raw["beats"][0]["key_info"][0]["discovery_effects"]["grant_items"] = []
        raw["beats"][1]["key_info"][0]["discovery_effects"]["grant_items"] = [
            {"item_id": "item_lens_shard", "quantity": 1}
        ]
        raw["beats"][3]["advance_conditions"][0].update(
            kind="item", predicate={"item_id": "item_lens_shard"}
        )
        from src.model.canon import Canon

        errors = validate_canon_playability(Canon.from_dict(raw))
        self.assertTrue(
            any("beat_archive" in error and "无法取得" in error for error in errors),
            errors,
        )

    async def test_assembly_repair_includes_both_duplicate_flag_writers(self):
        from src.story.generator import _repair_assembled_canon

        brief, _, plan, raw = generated_story()
        original = deepcopy(raw)
        raw["beats"][1]["key_info"][0]["discovery_effects"]["flags_set"][
            "flag_understood_ritual"
        ] = True
        completion = AsyncMock(
            return_value={
                "objects": {
                    f"beats:{raw['beats'][0]['id']}": original["beats"][0],
                    f"beats:{raw['beats'][1]['id']}": original["beats"][1],
                }
            }
        )
        with patch("src.story.generator._complete_json", completion):
            result, _, repairs = await _repair_assembled_canon(
                raw, brief=brief, plan=plan, stage_label="测试"
            )
        completion.assert_awaited_once()
        self.assertEqual(repairs, 1)
        self.assertEqual(_full_canon_errors(result, brief, plan)[1], [])

    async def test_repaired_snapshot_survives_interruption_before_final_review(self):
        brief, _, plan, raw = generated_story()
        artifacts = {
            "plan": plan.model_dump(),
            **{
                f"fragment:{kind}": value
                for kind, value in _canon_fragments(raw, plan).items()
            },
        }
        changed = {**raw["cast"][0], "secret": "新的角色动机"}

        async def save(stage, key, payload, attempt):
            artifacts[key] = deepcopy(payload)

        completion = AsyncMock(
            side_effect=[
                {
                    "passed": False,
                    "issues": [
                        {
                            "severity": "error",
                            "code": "motivation",
                            "message": "动机冲突",
                            "affected_object_ids": [changed["id"]],
                        }
                    ],
                },
                {"objects": {f"cast:{changed['id']}": changed}},
                StoryGenerationError("模拟复核时传输中断"),
            ]
        )
        with (
            patch("src.story.generator._complete_json", completion),
            self.assertRaisesRegex(StoryGenerationError, "传输中断"),
        ):
            await generate_staged_canon(
                confirmed_brief=brief, resume_artifacts=artifacts, on_artifact=save
            )
        self.assertTrue(artifacts["assembled_canon"]["continuity_repaired"])
        self.assertNotEqual(
            artifacts["fragment:cast"]["cast"][0]["secret"], changed["secret"]
        )
        completion = AsyncMock(return_value={"passed": True, "issues": []})
        with patch("src.story.generator._complete_json", completion):
            result, _, metrics = await generate_staged_canon(
                confirmed_brief=brief, resume_artifacts=artifacts, on_artifact=save
            )
        completion.assert_awaited_once()
        self.assertEqual(result["cast"][0]["secret"], changed["secret"])
        self.assertEqual(metrics.repair_count, 1)
        # 正文变动必须使之前的快照和报告失效；非法新报告不能落库。
        artifacts["fragment:cast"]["cast"][0]["secret"] = "另一个版本"
        artifacts.pop("continuity_review_final")
        old_review = deepcopy(artifacts["continuity_review"])
        invalid = {
            "passed": True,
            "issues": [{"severity": "error", "code": "bad", "message": "矛盾"}],
        }
        completion = AsyncMock(return_value=invalid)
        with (
            patch("src.story.generator._complete_json", completion),
            self.assertRaisesRegex(StoryGenerationError, "复核输出结构"),
        ):
            await generate_staged_canon(
                confirmed_brief=brief, resume_artifacts=artifacts, on_artifact=save
            )
        completion.assert_awaited_once()
        self.assertEqual(artifacts["continuity_review"], old_review)

    async def test_cancelled_wave_waits_for_siblings_and_does_not_persist_them(self):
        brief, _, plan, _ = generated_story()
        running = asyncio.Event()
        stopped = asyncio.Event()
        saved = AsyncMock()

        async def fragment(**kwargs):
            if kwargs["fragment_kind"] == "top_level":
                await running.wait()
                raise StoryGenerationError("首分片失败")
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with (
            patch("src.story.generator._generate_fragment", side_effect=fragment),
            self.assertRaisesRegex(StoryGenerationError, "首分片失败"),
        ):
            await generate_staged_canon(
                confirmed_brief=brief,
                resume_artifacts={"plan": plan.model_dump()},
                on_artifact=saved,
            )
        self.assertTrue(stopped.is_set())
        self.assertNotIn(
            "fragment:cast", [call.args[1] for call in saved.await_args_list]
        )

    async def test_consumed_item_blocks_later_gate_and_unlimited_grants_can_repeat(
        self,
    ):
        from src.model.canon import Canon

        _, _, _, raw = generated_story()
        raw["action_definitions"] = []
        for beat_index, flag in ((3, "first_gate"), (4, "second_gate")):
            raw["beats"][beat_index]["advance_conditions"][0].update(
                kind="flag", predicate={"flag": flag}
            )
            raw["action_definitions"].append(
                {
                    "id": flag,
                    "name": "用镜片开门",
                    "source_kind": "item",
                    "source_ref": "item_lens_shard",
                    "scopes": ["world"],
                    "requirements": {"beat_ids": [raw["beats"][beat_index]["id"]]},
                    "usage": {"kind": "consume_item", "quantity": 1},
                    "contract": {
                        "effect_templates": [
                            {
                                "id": "open_gate",
                                "kind": "set_flag",
                                "flag": flag,
                                "value": True,
                                "target_mode": "none",
                                "when": {"outcomes": ["always"]},
                            }
                        ]
                    },
                }
            )
        self.assertTrue(
            any(
                "beat_climax" in error and "无法取得" in error
                for error in validate_canon_playability(Canon.from_dict(raw))
            )
        )
        raw["action_definitions"].append(
            {
                "id": "make_lens",
                "name": "制作镜片",
                "source_kind": "quest_feature",
                "source_ref": "craft_lens",
                "scopes": ["world"],
                "usage": {"kind": "unlimited"},
                "contract": {
                    "effect_templates": [
                        {
                            "id": "grant_lens",
                            "kind": "grant_item",
                            "item_id": "item_lens_shard",
                            "quantity": 1,
                            "target_mode": "actor",
                            "when": {"outcomes": ["always"]},
                        }
                    ]
                },
            }
        )
        raw["action_definitions"][0]["usage"]["quantity"] = 3
        self.assertEqual(validate_canon_playability(Canon.from_dict(raw)), [])

    async def test_expired_task_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(canon_dir=Path(directory), db_path=Path(":memory:"))
            with closing(service._store):
                service._store.create_task("expired", _brief().model_dump())
                task = service._store.next_queued_task()
                service._store.task_deadline("expired", -1)
                completion = AsyncMock()
                with patch(
                    "src.services.story_service.generate_staged_canon", completion
                ):
                    await service._run_task(task)
                completion.assert_not_awaited()
                self.assertEqual(service._store.get_task("expired")["status"], "failed")


class StoryTaskPersistenceTests(unittest.TestCase):
    def test_deadline_and_budget_survive_restart_retry_and_terminal_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.sqlite3"
            store = StoryGenerationStore(path)
            store.create_task("task", {}, requester_key="owner")
            store.next_queued_task()
            deadline = store.task_deadline("task", 120)
            store.reserve_llm_call("task", limit=24)
            store.save_artifact(
                "task",
                stage="planning",
                artifact_key="core",
                payload={"ok": True},
                attempt=1,
            )
            store.update_task("task", stage="前进", progress=70)
            store.update_task("task", stage="另一分片", progress=30)
            store.close()
            store = StoryGenerationStore(path)
            store.recover_interrupted()
            store.next_queued_task()
            self.assertEqual(store.task_deadline("task", 120), deadline)
            self.assertEqual(store.get_task("task")["progress"], 70)
            store.mark_failed("task", "传输错误")
            with self.assertRaises(RuntimeError):
                store.save_artifact(
                    "task",
                    stage="compiling",
                    artifact_key="late",
                    payload={},
                    attempt=0,
                )
            with self.assertRaises(ValueError):
                store.retry_task("task", "someone_else", active_limit=20)
            retried = store.retry_task("task", "owner", active_limit=20)
            self.assertEqual(retried["llm_call_count"], 1)
            self.assertEqual(retried["repair_count"], 1)
            self.assertIsNone(retried["deadline_at"])
            store.mark_failed("task", "再次失败")
            with self.assertRaises(ValueError):
                store.retry_task("task", "owner", active_limit=20)
            store.close()


if __name__ == "__main__":
    unittest.main()
