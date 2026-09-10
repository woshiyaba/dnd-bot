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
    data["plan_version"] = 4
    for actor in data["entities"]["actors"]:
        actor["kind"] = "npc"
    for beat in data["beats"]:
        if beat["kind"] == "ending":
            beat["actor_ids"] = []
        beat["enemy_actor_ids"] = (
            list(beat["actor_ids"]) if beat.get("encounter_id") else []
        )
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
            "player_character_names": [],
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


def execution_candidate(plan):
    """模型输入只写剧情拍及固定双结局内容，不重复编写结局节点与路线。"""
    candidate = plan.model_dump(
        exclude_none=True, exclude={"entities", "ending_routes"}
    )
    endings = {beat.id: beat for beat in plan.beats if beat.kind == "ending"}
    candidate["endings"] = {
        route.ending_id: {
            "objective": endings[route.ending_id].objective,
            "estimated_minutes": endings[route.ending_id].estimated_minutes,
            "required_facts": route.required_facts,
            "payoffs": route.payoffs,
        }
        for route in plan.ending_routes
    }
    candidate["beats"] = [
        beat for beat in candidate["beats"] if beat["kind"] != "ending"
    ]
    return candidate


def model_responder(core, plan, raw, calls):
    fragments = _canon_fragments(raw, plan)

    async def respond(prompt, *, stage, call_context=None, **kwargs):
        if call_context:
            await call_context.reserve_call(stage)
        calls.append((stage, len(prompt)))
        if stage == "计划 故事核心与章节":
            return core.model_dump()
        if stage == "计划 角色与地点名册":
            return plan.entities.model_dump(exclude_none=True)
        if stage == "计划":
            return execution_candidate(plan)
        if stage.startswith("分片 "):
            fragment = deepcopy(fragments[stage.removeprefix("分片 ")])
            for beat in fragment.get("beats", []):
                for actor in beat["entry_state"]["actors"]:
                    for key in ("name", "type", "card"):
                        actor.pop(key, None)
            return fragment
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
            truncated = len(requests) == 5
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
                    ModelRole.STORY_REPAIR,
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
        self.assertEqual(len(requests), 6)
        self.assertTrue(
            all("JSON" in request["messages"][0]["content"] for request in requests)
        )
        self.assertTrue(all(request["max_tokens"] == 8192 for request in requests))
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertEqual(requests[1]["thinking"], {"type": "disabled"})
        self.assertEqual(requests[2]["reasoning_effort"], "low")
        self.assertEqual(requests[3]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", requests[3])
        self.assertEqual(requests[5]["thinking"], {"type": "disabled"})

    async def test_invalid_actor_type_is_compiled_without_model_repair(self):
        from src.story.generator import _generate_fragment
        from src.model.dm_state import build_beat_scene

        brief, _, plan, raw = generated_story()
        kind = f"act:{plan.acts[0].id}"
        compiled = _canon_fragments(raw, plan)
        fragment = deepcopy(compiled[kind])
        for beat in fragment["beats"]:
            for clue in beat["key_info"]:
                clue.setdefault("discovery_effects", {}).setdefault("flags_set", {})[
                    clue["id"]
                ] = True
            for actor in beat["entry_state"]["actors"]:
                actor.update(
                    type="player", name="被错误重写的名字", card={"current_hp": 999}
                )
        completion = AsyncMock(return_value=fragment)
        with patch("src.story.generator._complete_json", completion):
            result, repairs = await _generate_fragment(
                fragment_kind=kind,
                brief=brief,
                plan=plan,
                registry=story_plan_id_registry(plan),
                ledger=[item.model_dump() for item in plan.effect_owner_ledger],
                reference_fragments=[],
                adjacent_fragments=[],
                compiled_fragments=compiled,
            )
        completion.assert_awaited_once()
        self.assertEqual(repairs, 0)
        self.assertEqual(
            _fragment_errors(
                kind, result, plan, story_plan_id_registry(plan), compiled
            ),
            [],
        )
        schema = completion.await_args.kwargs["schema"].model_json_schema()
        self.assertEqual(
            set(schema["$defs"]["CanonActorPlacement"]["properties"]),
            {"actor_id", "disposition", "location_id"},
        )
        raw["beats"] = [
            next((item for item in result["beats"] if item["id"] == beat["id"]), beat)
            for beat in raw["beats"]
        ]
        canon, errors = _full_canon_errors(raw, brief, plan)
        self.assertEqual(errors, [])
        scene = build_beat_scene(canon, canon.beat(result["beats"][0]["id"]))
        for actor in scene["actors"]:
            self.assertEqual(actor["card"], canon.npc(actor["actor_id"]).card)
        repaired = deepcopy(result["beats"][0])
        for actor in repaired["entry_state"]["actors"]:
            actor["type"] = "enemy"
        with patch(
            "src.story.generator._complete_json",
            return_value={"objects": {f"beats:{repaired['id']}": repaired}},
        ):
            result = await _repair_canon_objects(
                raw,
                ids={repaired["id"]},
                errors=["修复文字"],
                brief=brief,
                plan=plan,
                call_context=None,
            )
        self.assertEqual(_full_canon_errors(result, brief, plan)[1], [])

    async def test_player_roster_and_missing_enemy_fail_before_compilation(self):
        from src.story.generator import _validate_story_plan_candidate

        brief, _, plan, _ = generated_story()
        data = plan.model_dump()
        data["entities"]["actors"][0]["kind"] = "player"
        next(beat for beat in data["beats"] if beat["encounter_id"])[
            "enemy_actor_ids"
        ] = []
        _, issues, _ = _validate_story_plan_candidate(data, brief)
        self.assertTrue(
            any(
                "玩家角色" in issue.message and issue.category == "structural"
                for issue in issues
            )
        )
        self.assertTrue(
            any(
                "缺少明确敌方名单" in issue.message and issue.category == "structural"
                for issue in issues
            )
        )
        with patch("src.story.generator._complete_json", AsyncMock()) as completion:
            with self.assertRaisesRegex(StoryGenerationError, "已持久化 StoryPlan"):
                await generate_staged_canon(
                    confirmed_brief=brief, resume_artifacts={"plan": data}
                )
        completion.assert_not_awaited()

    async def test_plan_trigger_ids_are_compiled_before_strict_validation(self):
        from src.story.generator import _validate_story_plan_candidate
        from src.schemas.story import CanonTriggerDraft

        brief, _, plan, _ = generated_story()
        raw = plan.model_dump(exclude_none=True)
        ending_minutes = next(
            beat["estimated_minutes"]
            for beat in raw["beats"]
            if beat["id"] == "ending_win"
        )
        raw["beats"][0]["estimated_minutes"] += ending_minutes - 1
        for beat in raw["beats"]:
            if beat["kind"] == "ending":
                beat["estimated_minutes"] = 0
                beat["pressure"] = ""
            for exit_ in beat["exits"]:
                exit_["trigger"].pop("id")
        raw["win_condition"].pop("id")
        raw["lose_condition"].pop("id")
        climax = next(beat for beat in raw["beats"] if beat["kind"] == "climax")
        climax["exits"] = []
        climax["fail_forward"] = ""
        normalized, issues, _ = _validate_story_plan_candidate(raw, brief)
        self.assertEqual(issues, [])
        self.assertEqual(normalized.win_condition.id, "win_condition")
        compiled_climax = next(
            beat for beat in normalized.beats if beat.kind == "climax"
        )
        self.assertEqual(compiled_climax.exits[0].to_beat_id, "ending_win")
        self.assertEqual(
            compiled_climax.exits[0].trigger.predicate,
            normalized.win_condition.predicate,
        )
        self.assertTrue(
            all(
                beat.estimated_minutes == 1
                for beat in normalized.beats
                if beat.kind == "ending"
            )
        )
        for beat in normalized.beats:
            for index, exit_ in enumerate(beat.exits, 1):
                self.assertEqual(exit_.trigger.id, f"trigger_{beat.id}_{index}")
        with self.assertRaises(ValidationError):
            CanonTriggerDraft.model_validate(raw["win_condition"])
        raw["beats"][0]["exits"][0]["trigger"] = {
            "kind": "location",
            "predicate": {"location_id": raw["beats"][0]["location_ids"][0]},
        }
        self.assertTrue(
            any(
                "初始地点" in issue.message
                for issue in _validate_story_plan_candidate(raw, brief)[1]
            )
        )

    async def test_fixed_ending_slots_remove_dangling_ending_ids_without_replanning(
        self,
    ):
        from src.schemas.story import story_execution_plan_schema
        from src.story.generator import (
            _generate_story_plan,
            _validate_story_plan_candidate,
        )

        brief, core, plan, _ = generated_story()
        broken = plan.model_dump(exclude_none=True)
        for beat in broken["beats"]:
            if beat["kind"] == "ending":
                beat["id"] += "_alias"
        _, issues, _ = _validate_story_plan_candidate(broken, brief)
        self.assertTrue(
            any("不存在的 Beat «ending_win»" in issue.message for issue in issues)
        )

        candidate = execution_candidate(plan)
        for beat in candidate["beats"]:
            if beat["kind"] == "climax":
                beat["exits"] = []
        roster = plan.entities.model_dump(exclude_none=True)
        schema = story_execution_plan_schema(roster)
        schema.model_validate(candidate)
        for change in ("missing", "alias", "duplicate"):
            with self.subTest(change=change):
                invalid = deepcopy(candidate)
                if change == "missing":
                    invalid["endings"].pop("ending_win")
                elif change == "alias":
                    invalid["endings"]["ending_victory"] = invalid["endings"].pop(
                        "ending_win"
                    )
                else:
                    invalid["beats"].append(
                        next(
                            beat.model_dump()
                            for beat in plan.beats
                            if beat.kind == "ending"
                        )
                    )
                with self.assertRaises(ValidationError):
                    schema.model_validate(invalid)

        with patch(
            "src.story.generator._complete_json", return_value=candidate
        ) as completion:
            compiled, repairs = await _generate_story_plan(
                brief, [], story_core=core.model_dump(), frozen_entities=roster
            )
        completion.assert_awaited_once()
        self.assertEqual(repairs, 0)
        self.assertEqual(compiled.ending_routes, plan.ending_routes)
        self.assertEqual(
            {
                beat.id: beat.objective
                for beat in compiled.beats
                if beat.kind == "ending"
            },
            {beat.id: beat.objective for beat in plan.beats if beat.kind == "ending"},
        )
        _, issues, _ = _validate_story_plan_candidate(compiled.model_dump(), brief)
        self.assertEqual(issues, [])

    async def test_missing_ending_slot_replans_before_local_section_merge(self):
        from src.story.generator import _generate_story_plan

        brief, core, plan, _ = generated_story()
        candidate = execution_candidate(plan)
        for change in ("slot", "all", "null"):
            with self.subTest(change=change):
                invalid = deepcopy(candidate)
                if change == "slot":
                    invalid["endings"].pop("ending_win")
                elif change == "all":
                    invalid.pop("endings")
                else:
                    invalid["endings"] = None
                with patch(
                    "src.story.generator._complete_json",
                    side_effect=[invalid, deepcopy(candidate)],
                ) as completion:
                    compiled, repairs = await _generate_story_plan(
                        brief,
                        [],
                        story_core=core.model_dump(),
                        frozen_entities=plan.entities.model_dump(exclude_none=True),
                    )
                self.assertEqual(repairs, 1)
                self.assertEqual(
                    completion.await_args.kwargs["stage"], "计划结构重规划"
                )
                self.assertEqual(compiled.ending_routes, plan.ending_routes)

    async def test_plan_repair_corrects_invalid_envelope_before_merge(self):
        from src.story.generator import _generate_story_plan

        brief, _, plan, _ = generated_story()
        raw = plan.model_dump(exclude_none=True)
        valid_beats = deepcopy(raw["beats"])
        raw["beats"][0]["pressure"] = ""
        completion = AsyncMock(
            side_effect=[
                raw,
                {
                    "repair_kind": "story_plan_sections",
                    "sections": {"beats": {"beats": valid_beats}},
                },
                {
                    "repair_kind": "story_plan_sections",
                    "sections": {"beats": valid_beats},
                },
            ]
        )
        with patch("src.story.generator._complete_json", completion):
            repaired, repairs = await _generate_story_plan(brief, [])
        self.assertEqual(completion.await_count, 3)
        self.assertEqual(repairs, 1)
        self.assertEqual(repaired.beats[0].pressure, plan.beats[0].pressure)

    async def test_frozen_roster_is_reused_and_cannot_be_rewritten_by_plan(self):
        from src.story.generator import _generate_story_plan, _entity_roster_errors
        from src.schemas.story import (
            story_entity_roster_schema,
            story_execution_plan_schema,
        )

        brief, core, plan, raw = generated_story()
        roster = plan.entities.model_dump(exclude_none=True)
        self.assertTrue(
            _entity_roster_errors(plan.entities, set(), {plan.entities.actors[0].name})
        )
        candidate = execution_candidate(plan)
        candidate["entities"] = {"clues": [{"id": "clue_invented"}]}
        with patch(
            "src.story.generator._complete_json", return_value=candidate
        ) as completion:
            result, repairs = await _generate_story_plan(
                brief, [], frozen_entities=roster
            )
        self.assertEqual(result.entities, plan.entities)
        self.assertEqual(repairs, 0)
        self.assertNotIn(
            "entities", completion.await_args.kwargs["schema"].model_fields
        )
        schema = story_entity_roster_schema(
            brief.scale_profile.locations,
            brief.scale_profile.encounters,
            brief.scale_profile.clues,
        )
        invalid_roster = deepcopy(roster)
        invalid_roster["clues"].pop()
        with self.assertRaises(ValidationError):
            schema.model_validate(invalid_roster)
        empty_resources = {**roster, "flags": [], "items": []}
        execution_schema = story_execution_plan_schema(empty_resources)
        self.assertNotIn("foreshadowing_payoffs", execution_schema.model_fields)
        self.assertNotIn("effect_owner_ledger", execution_schema.model_fields)
        bound_beat = execution_schema.model_fields["beats"].annotation.__args__[0]
        invalid_beat = plan.beats[0].model_dump()
        invalid_beat["actor_ids"] = ["actor_invented"]
        with self.assertRaises(ValidationError):
            bound_beat.model_validate(invalid_beat)
        invalid_beat = plan.beats[0].model_dump()
        invalid_beat["exits"][0]["trigger"] = {
            "kind": "semantic",
            "predicate": {
                "prompt": "离开现场",
                "location_id": roster["locations"][0]["id"],
            },
        }
        with self.assertRaises(ValidationError):
            bound_beat.model_validate(invalid_beat)
        calls = []
        with patch(
            "src.story.generator._complete_json",
            side_effect=model_responder(core, plan, raw, calls),
        ):
            await generate_staged_canon(
                confirmed_brief=brief,
                resume_artifacts={
                    "plan:core": core.model_dump(),
                    "plan:entities": roster,
                },
            )
        self.assertEqual(calls[0][0], "计划")
        self.assertFalse(
            any(
                stage in {"计划 故事核心与章节", "计划 角色与地点名册"}
                for stage, _ in calls
            )
        )
        candidate = execution_candidate(plan)
        candidate["acts"] = []
        for beat in candidate["beats"]:
            beat["act_id"] = "act_wrong"
        with patch(
            "src.story.generator._complete_json", return_value=candidate
        ) as completion:
            result, repairs = await _generate_story_plan(
                brief, [], story_core=core.model_dump(), frozen_entities=roster
            )
        self.assertEqual(
            [beat.act_id for beat in result.beats], [beat.act_id for beat in plan.beats]
        )
        self.assertEqual(repairs, 0)
        completion.assert_awaited_once()

    async def test_full_validation_rejects_actor_and_enemy_drift(self):
        brief, _, plan, raw = generated_story()
        beat = next(beat for beat in raw["beats"] if beat.get("encounter"))
        beat["entry_state"]["actors"][0]["type"] = "monster"
        beat["encounter"]["monster_ids"] = [plan.entities.actors[0].id]
        errors = _full_canon_errors(raw, brief, plan)[1]
        self.assertTrue(any("身份与类型" in error for error in errors), errors)
        self.assertTrue(any("敌方名单" in error for error in errors), errors)

    async def test_battle_failure_must_have_global_route_and_hints_remain_repairable(
        self,
    ):
        from src.story.generator import _validate_story_plan_candidate
        from src.session.story_nodes import evaluate_advancement, transition_to_beat

        brief, _, plan, raw = generated_story()
        data = plan.model_dump()
        data["lose_condition"]["predicate"]["encounter_id"] = next(
            beat.encounter_id for beat in plan.beats if beat.encounter_id
        )
        _, issues, _ = _validate_story_plan_candidate(data, brief)
        self.assertTrue(any("lose_condition" in issue.message for issue in issues))
        target = raw["beats"][0]
        corrected = deepcopy(target)
        corrected["stuck_fallback"][
            "hint"
        ] = "调查受挫时寻找旁证；若队伍战败则进入失败结局。"
        with patch(
            "src.story.generator._complete_json",
            return_value={"objects": {f"beats:{target['id']}": corrected}},
        ):
            repaired = await _repair_canon_objects(
                raw,
                ids={target["id"]},
                errors=["失败提示不可执行"],
                brief=brief,
                plan=plan,
                call_context=None,
            )
        self.assertEqual(
            repaired["beats"][0]["stuck_fallback"]["hint"],
            corrected["stuck_fallback"]["hint"],
        )
        self.assertEqual(_full_canon_errors(repaired, brief, plan)[1], [])
        top = deepcopy(_canon_fragments(raw, plan)["top_level"])
        top["lose_condition"]["predicate"]["encounter_id"] = "enc_invented"
        with patch(
            "src.story.generator._complete_json",
            return_value={"objects": {"top_level": top}},
        ):
            repaired_top = await _repair_canon_objects(
                raw,
                ids={"top_level"},
                errors=["调整顶层说明"],
                brief=brief,
                plan=plan,
                call_context=None,
            )
        self.assertEqual(repaired_top["lose_condition"], raw["lose_condition"])
        canon, _ = _full_canon_errors(repaired, brief, plan)
        with patch("src.session.story_nodes.current_canon", return_value=canon):
            for beat in canon.beats:
                if beat.encounter is None:
                    continue
                result = await evaluate_advancement(
                    {
                        "story": {"current_beat_id": beat.id},
                        "last_combat": {
                            "encounter_id": beat.encounter.id,
                            "outcome": "players_lose",
                        },
                    }
                )
                self.assertEqual(result["story"]["pending_next_beat_id"], "ending_lose")
                scene = {
                    "location_id": beat.location_ids[0],
                    "actors": [{"actor_id": beat.encounter.monster_ids[0]}],
                }
                transitioned = transition_to_beat(
                    {"story": {"current_beat_id": beat.id}, "scene": scene},
                    "ending_lose",
                )
                self.assertEqual(
                    transitioned["scene"]["location_id"], scene["location_id"]
                )
                self.assertEqual(transitioned["scene"]["actors"], scene["actors"])

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
                from fastapi import HTTPException

                stale = deepcopy(artifacts)
                stale["continuity_review"]["canon_hash"] = "old_content"
                with (
                    patch.object(service._store, "artifacts", return_value=stale),
                    self.assertRaisesRegex(HTTPException, "复核"),
                ):
                    await service.publish(task["draft_id"])
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

    async def test_unreachable_clue_repairs_map_without_moving_story_objects(self):
        from src.story.generator import _repair_assembled_canon
        from src.story.prompt import build_fragment_prompt

        brief, _, plan, raw = generated_story()
        start, destination, outside = [
            location["id"] for location in raw["locations"][:3]
        ]
        first = raw["beats"][0]
        plan.beats[0].location_ids = [start, destination]
        first["location_ids"] = [start, destination]
        first["key_info"][0]["location_id"] = destination
        first["entry_state"]["exits"] = [destination]
        locations = {location["id"]: location for location in raw["locations"]}
        locations[start]["intra_exits"] = [outside]
        locations[outside]["intra_exits"] = [destination]
        locations[destination]["intra_exits"] = [start]
        original = deepcopy(raw)
        _, errors = _full_canon_errors(raw, brief, plan)
        self.assertTrue(any("无法从入场地点" in error for error in errors), errors)

        async def repair(prompt, *, schema, **kwargs):
            targets = schema.model_fields["objects"].annotation.model_fields
            self.assertEqual(
                {field.alias for field in targets.values()},
                {f"locations:{start}", f"locations:{destination}"},
            )
            context = json.loads(
                prompt.split("<readonly_location_context>")[1].split(
                    "</readonly_location_context>"
                )[0]
            )
            self.assertEqual(context["locations"], raw["locations"])
            self.assertEqual(context["beat_routes"][0]["entry_location_id"], start)
            return {
                "objects": {
                    f"locations:{start}": {
                        **locations[start],
                        "intra_exits": [outside, destination],
                    },
                    f"locations:{destination}": locations[destination],
                }
            }

        with patch(
            "src.story.generator._complete_json", side_effect=repair
        ) as completion:
            result, canon, repairs = await _repair_assembled_canon(
                raw, brief=brief, plan=plan, stage_label="测试地图修复"
            )
        completion.assert_awaited_once()
        self.assertEqual(repairs, 1)
        self.assertEqual(result["beats"], original["beats"])
        self.assertEqual(result["locations"][2:], original["locations"][2:])
        self.assertEqual(_full_canon_errors(result, brief, plan)[1], [])
        self.assertIn(destination, canon.location(start).intra_exits)
        one_way = deepcopy(result)
        one_way["locations"][1]["intra_exits"] = []
        self.assertEqual(_full_canon_errors(one_way, brief, plan)[1], [])

        prompt = build_fragment_prompt(
            fragment_kind="locations",
            confirmed_brief=brief,
            story_plan=plan.model_dump(),
            id_registry=story_plan_id_registry(plan),
            effect_owner_ledger=[],
            reference_fragments=[],
        )
        context = json.loads(
            prompt.split("<validated_story_plan>")[1].split("</validated_story_plan>")[
                0
            ]
        )
        self.assertEqual(
            context["beat_location_scopes"][0],
            {"beat_id": first["id"], "location_ids": [start, destination]},
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
                            "affected_act_ids": [plan.acts[0].id],
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
        self.assertEqual(
            artifacts["assembled_canon"]["continuity_repair_ids"], [changed["id"]]
        )
        self.assertIn("<previous_review>", completion.await_args_list[2].args[0])
        self.assertIn("动机冲突", completion.await_args_list[2].args[0])
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
    def test_old_failed_task_requests_regeneration_without_modifying_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            service = StoryService(
                canon_dir=Path(directory), db_path=Path(directory) / "tasks.sqlite3"
            )
            with closing(service._store):
                service._store.create_task("old", _brief().model_dump())
                service._store.save_artifact(
                    "old",
                    stage="planning",
                    artifact_key="generation_context",
                    payload={"version": 2},
                    attempt=0,
                )
                service._store.mark_failed("old", "旧失败")
                response = service.get_generation_task("old")
                self.assertFalse(response.can_retry)
                self.assertIn("按已确认的设计稿重新生成", response.error)
                self.assertEqual(
                    service._store.artifacts("old"),
                    {"generation_context": {"version": 2}},
                )

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
