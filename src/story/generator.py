"""使用真实 LLM 执行故事访谈、Canon 编译和校验修复。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import ModelRole, get_chat_model, get_model_name
from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import (
    PlanBatch,
    PlanBeatDetail,
    PlanBeatOutline,
    PlanBeatPlacement,
    PlanBranchBlueprint,
    PlanClueDetail,
    PlanComplexBatch,
    PlanEffectOwnerChoice,
    PlanEndingDetail,
    PlanEntityBudget,
    PlanEntityDraft,
    PlanEntity,
    PlanPayoffDetail,
    PlanRouteText,
    PlanSimpleBatch,
    StoryDesignBrief,
    StoryInterviewResponse,
    StoryPlan,
    StoryPlanFrame,
    StoryPlanWorkState,
    StoryQualityMetrics,
)
from src.story.prompt import (
    build_canon_authoring_prompt,
    build_canon_repair_prompt,
    build_continuity_repair_prompt,
    build_continuity_review_prompt,
    build_fragment_prompt,
    build_fragment_repair_prompt,
    build_story_plan_prompt,
    build_story_plan_repair_prompt,
    build_story_plan_replan_prompt,
    build_story_plan_stage_prompt,
    build_story_plan_stage_repair_prompt,
    build_story_interview_prompt,
    build_story_interview_repair_prompt,
    normalize_confirmed_design_brief,
)
from src.story.plan_normalizer import (
    normalize_story_plan_candidate,
    story_plan_normalization_changes,
)
from src.story.plan_repair import (
    PlanValidationIssue,
    affected_story_plan_sections,
    merge_story_plan_sections,
    story_plan_field_issues,
    story_plan_issue_fingerprint,
)
from src.story.validation import (
    canon_quality_metrics,
    story_plan_id_registry,
    validate_fragment_ids,
    validate_fragment_runtime,
    validate_effect_owner_ledger,
    validate_generated_canon,
    validate_story_plan_issues,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANON_DIR = PROJECT_ROOT / "canon"
REFERENCE_CANON_PATHS = (
    CANON_DIR / "prodigal_return_quest.json",
    CANON_DIR / "whispers_bell_tower.json",
)
MAX_REPAIR_ATTEMPTS = 10
MAX_STORY_PLAN_LOCAL_REPAIRS = 2
MAX_STORY_PLAN_REPLANS = 1
MAX_ASSEMBLY_REPAIRS = 2

ArtifactCallback = Callable[[str, str, dict[str, Any], int], Awaitable[None]]
StageStartCallback = Callable[[str], Awaitable[None]]


class StoryGenerationError(RuntimeError):
    """真实 LLM 未能返回可用的故事结构或 Canon。"""


def _load_reference_canons() -> list[dict[str, Any]]:
    """每次编译重新读取内置 Canon，使故事框架改动立即进入生成上下文。"""
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in REFERENCE_CANON_PATHS
    ]


def _load_reference_fragments() -> list[dict[str, Any]]:
    """从两份短篇参考自动提取依赖闭合的功能片段，避免传送两份完整 Canon。"""
    fragments: list[dict[str, Any]] = []
    for raw in _load_reference_canons():
        action_beat_ids = {
            str(beat_id)
            for action in raw.get("action_definitions", [])
            for beat_id in (action.get("requirements") or {}).get("beat_ids", [])
        }
        candidates: list[tuple[dict[str, Any], set[str]]] = []
        for beat in raw.get("beats", []):
            functions: set[str] = set()
            if len(beat.get("location_ids", [])) > 1:
                functions.add("multi_location_exploration")
            encounter = beat.get("encounter") or {}
            if encounter.get("on_win_discoveries"):
                functions.add("post_combat_discovery")
            if beat.get("id") in action_beat_ids:
                functions.add("hard_gate_and_rule_action")
            if beat.get("kind") == "climax" and encounter:
                functions.add("boss_settlement")
            if functions:
                candidates.append((beat, functions))
        # 贪心覆盖功能类别；同分时保持 Canon 原顺序，通常每份只留下 1～2 拍。
        uncovered = {
            "multi_location_exploration",
            "post_combat_discovery",
            "hard_gate_and_rule_action",
            "boss_settlement",
        }
        selected_ids: set[str] = set()
        selected_functions: set[str] = set()
        while candidates and uncovered:
            index, (beat, functions) = max(
                enumerate(candidates),
                key=lambda item: (len(item[1][1] & uncovered), -item[0]),
            )
            covered = functions & uncovered
            if not covered:
                break
            selected_ids.add(str(beat.get("id")))
            selected_functions.update(functions)
            uncovered -= covered
            candidates.pop(index)
        beats = [
            beat for beat in raw.get("beats", []) if beat.get("id") in selected_ids
        ]
        location_ids = {
            location_id
            for beat in beats
            for location_id in beat.get("location_ids", [])
        }
        actor_ids = {
            actor.get("actor_id") or actor.get("npc_ref")
            for beat in beats
            for actor in (beat.get("entry_state") or {}).get("actors", [])
        }
        encounter_ids = {
            beat["encounter"]["id"]
            for beat in beats
            if isinstance(beat.get("encounter"), dict) and beat["encounter"].get("id")
        }
        actions = [
            action
            for action in raw.get("action_definitions", [])
            if selected_ids.intersection(
                (action.get("requirements") or {}).get("beat_ids", [])
            )
            or encounter_ids.intersection(
                (action.get("requirements") or {}).get("encounter_ids", [])
            )
        ]
        external_beat_ids = {
            str(exit_.get("next_beat_id"))
            for beat in beats
            for exit_ in beat.get("exits", [])
            if str(exit_.get("next_beat_id")) not in selected_ids
        }
        external_beats = [
            {
                "id": beat.get("id"),
                "kind": beat.get("kind"),
                "ending_outcome": beat.get("ending_outcome"),
            }
            for beat in raw.get("beats", [])
            if beat.get("id") in external_beat_ids
        ]
        fragments.append(
            {
                "source": raw.get("campaign_id"),
                "functions": sorted(selected_functions),
                "declared_flags": raw.get("declared_flags", []),
                "cast": [
                    item for item in raw.get("cast", []) if item.get("id") in actor_ids
                ],
                "locations": [
                    item
                    for item in raw.get("locations", [])
                    if item.get("id") in location_ids
                ],
                "action_definitions": actions,
                "beats": beats,
                "external_beat_stubs": external_beats,
                "win_condition": (
                    raw.get("win_condition")
                    if (raw.get("win_condition") or {})
                    .get("predicate", {})
                    .get("encounter_id")
                    in encounter_ids
                    else None
                ),
            }
        )
    return fragments


def _message_text(message: Any) -> str:
    """兼容字符串和分段内容，提取模型回复文本。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


async def _complete_json(
    prompt: str,
    *,
    stage: str,
    role: ModelRole,
    schema: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """调用真实 LLM 获取 JSON；提供 Schema 时交给 LangChain 约束输出。

    无 Schema 时也用 ``response_format={"type": "json_object"}`` 强制供应商只返回
    合法 JSON 对象（无 Markdown 围栏、无前后缀正文），避免依赖正则从自由文本里抠 JSON。
    """
    model_name = get_model_name(role)
    try:
        model = get_chat_model(model_name)
        if schema is not None:
            # DeepSeek 思考模式不接受 LangChain 强制函数选择；JSON mode
            # 由供应商保证 JSON 语法，再由下方 Pydantic 执行业务结构校验。
            completion_model = model.with_structured_output(schema, method="json_mode")
        else:
            # 分片、计划小阶段、连贯性复核等没有 Pydantic 模型的产物，也强制 JSON
            # 对象语法，把「输出不是 JSON」这类不稳定降到最低。
            completion_model = model.bind(response_format={"type": "json_object"})
        response = await completion_model.ainvoke(prompt)
    except Exception as exc:
        logger.exception(
            "[story_generator] LLM 调用失败 | stage=%s | model=%s",
            stage,
            model_name,
        )
        raise StoryGenerationError(f"故事 {stage} 的 LLM 调用失败：{exc}") from exc
    if schema is not None:
        try:
            structured = (
                response
                if isinstance(response, schema)
                else schema.model_validate(response)
            )
        except (TypeError, ValidationError) as exc:
            raise StoryGenerationError(
                f"故事 {stage} 的 LLM 输出不符合 {schema.__name__}：{exc}"
            ) from exc
        return structured.model_dump()
    parsed = extract_json_object(_message_text(response))
    if parsed is None:
        raise StoryGenerationError(f"故事 {stage} 的 LLM 输出不是可解析的 JSON 对象")
    return parsed


def _log_repair_attempt(
    *,
    stage: str,
    repair_round: int,
    errors: list[str],
    prompt: str,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> None:
    """记录修复轮次、校验问题和发送给修复模型的完整提示词。"""
    formatted_errors = "\n".join(
        f"  {index}. {error}" for index, error in enumerate(errors, start=1)
    )
    logger.info(
        "[story_generator] 开始%s | 修复轮次=%d/%d | 待修复问题=%d 个\n"
        "待修复问题：\n%s\n"
        "系统提示词：\n%s",
        stage,
        repair_round,
        max_attempts,
        len(errors),
        formatted_errors,
        prompt,
    )


def _story_plan_errors(plan: StoryPlan, brief: StoryDesignBrief) -> list[str]:
    """执行计划校验，并兼容分支 schema 与旧确认稿的最小并行预算语义。"""
    return [issue.message for issue in _story_plan_issues(plan, brief)]


def _story_plan_issues(
    plan: StoryPlan, brief: StoryDesignBrief
) -> list[PlanValidationIssue]:
    """执行结构化计划校验并兼容旧确认稿的并行预算语义。"""
    issues = validate_story_plan_issues(plan, brief)
    budget = brief.branching_budget
    if (
        budget is not None
        and budget.meaningful_branch_points > 0
        and budget.max_parallel_beats == 1
    ):
        # PlanBranchPoint.choices 的 schema 至少要求两条路线；旧确认稿里的 1 表示
        # 每条路线只占一个并行 Beat，而不是只允许一个 choice。若不在生成边界兼容，
        # 任意合法分支都会永久得到这一条互相矛盾的错误。
        issues = [
            issue for issue in issues if "超过 max_parallel_beats" not in issue.message
        ]
    return issues


async def continue_interview(
    *, conversation: list[dict[str, Any]], design_brief: dict[str, Any]
) -> StoryInterviewResponse:
    """继续一轮玩家故事访谈并校验结构化响应。"""
    raw = await _complete_json(
        build_story_interview_prompt(
            conversation=conversation,
            design_brief=design_brief,
        ),
        stage="访谈",
        role=ModelRole.STORY_INTERVIEW,
        schema=StoryInterviewResponse,
    )
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            return StoryInterviewResponse.model_validate(raw)
        except ValidationError as exc:
            errors = _story_interview_validation_errors(exc)
            if attempt == MAX_REPAIR_ATTEMPTS:
                raise StoryGenerationError(
                    f"故事访谈输出在 {MAX_REPAIR_ATTEMPTS} 次修复后仍不合法："
                    + "；".join(errors)
                ) from exc
        repair_round = attempt + 1
        repair_prompt = build_story_interview_repair_prompt(
            conversation=conversation,
            design_brief=design_brief,
            invalid_response=raw,
            validation_errors=errors,
        )
        _log_repair_attempt(
            stage="故事访谈修复",
            repair_round=repair_round,
            errors=errors,
            prompt=repair_prompt,
        )
        raw = await _complete_json(
            repair_prompt,
            stage=f"访谈修复（第 {repair_round} 次）",
            role=ModelRole.STORY_REPAIR,
            schema=StoryInterviewResponse,
        )

    raise AssertionError("故事访谈修复循环未按预期结束")


def _story_interview_validation_errors(exc: ValidationError) -> list[str]:
    """把 Pydantic 错误压缩成可交给修复模型的稳定字段路径。"""
    errors: list[str] = []
    for item in exc.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        errors.append(
            f"{location}: {item.get('msg', '校验失败')} "
            f"[{item.get('type', 'validation_error')}]"
        )
    return errors


def _canon_errors(draft: dict[str, Any]) -> tuple[Canon | None, list[str]]:
    """构造并校验 Canon，把字段解析异常转成可交给修复模型的错误。"""
    try:
        canon = Canon.from_dict(draft)
        errors = [*validate_canon(canon), *validate_authored_canon(canon)]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return None, [f"Canon 字段无法解析：{exc}"]
    return canon, errors


async def generate_canon(
    *, confirmed_brief: dict[str, Any]
) -> tuple[dict[str, Any], Canon]:
    """编译并确定性校验 Canon，最多让真实 LLM 修复两轮。"""
    reference_canons = _load_reference_canons()
    reserved_ids = sorted(path.stem for path in CANON_DIR.glob("*.json"))
    draft = await _complete_json(
        build_canon_authoring_prompt(
            confirmed_brief=confirmed_brief,
            reference_canons=reference_canons,
            reserved_campaign_ids=reserved_ids,
        ),
        stage="编译",
        role=ModelRole.STORY_AUTHORING,
    )

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        canon, errors = _canon_errors(draft)
        if canon is not None and not errors:
            return draft, canon
        if attempt == MAX_REPAIR_ATTEMPTS:
            raise StoryGenerationError(
                "Canon 在两次修复后仍未通过校验：" + "；".join(errors)
            )
        draft = await _complete_json(
            build_canon_repair_prompt(draft, errors),
            stage=f"修复（第 {attempt + 1} 次）",
            role=ModelRole.STORY_REPAIR,
        )

    raise AssertionError("Canon 修复循环未按预期结束")


async def generate_staged_canon(
    *,
    confirmed_brief: dict[str, Any] | StoryDesignBrief,
    reserved_campaign_ids: list[str] | None = None,
    resume_artifacts: dict[str, dict[str, Any]] | None = None,
    on_artifact: ArtifactCallback | None = None,
    on_stage_start: StageStartCallback | None = None,
    initial_repair_count: int = 0,
) -> tuple[dict[str, Any], Canon, StoryQualityMetrics]:
    """严格执行 StoryPlan → 分片 → 全校验 → 连贯性复核 → 定向修复。"""
    brief = normalize_confirmed_design_brief(confirmed_brief)
    reserved = sorted(
        set(reserved_campaign_ids or [])
        | {path.stem for path in CANON_DIR.glob("*.json")}
    )
    artifacts = dict(resume_artifacts or {})
    total_repairs = max(0, initial_repair_count)

    resumed_plan = "plan" in artifacts
    if resumed_plan:
        plan = StoryPlan.model_validate(artifacts["plan"])
        plan_errors = _story_plan_errors(plan, brief)
        if plan_errors:
            raise StoryGenerationError(
                "已持久化 StoryPlan 校验失败：" + "；".join(plan_errors)
            )
    else:
        plan, repairs = await _generate_story_plan_progressively(
            brief,
            reserved,
            artifacts=artifacts,
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        total_repairs += repairs
        if on_artifact:
            # 小阶段已经分别累计修复次数；最终聚合 artifact 不重复计数。
            await on_artifact("planning", "plan", plan.model_dump(), 0)
        artifacts["plan"] = plan.model_dump()

    registry = story_plan_id_registry(plan)
    plan_data = plan.model_dump()
    ledger = [item.model_dump() for item in plan.effect_owner_ledger]
    references = _load_reference_fragments()
    fragments: dict[str, dict[str, Any]] = {}

    fragment_order = ["top_level", "cast", "locations"]
    fragment_order.extend(f"act:{act.id}" for act in plan.acts)
    fragment_order.extend(["actions", "endings"])
    for fragment_kind in fragment_order:
        artifact_key = f"fragment:{fragment_kind}"
        if artifact_key in artifacts:
            fragment = artifacts[artifact_key]
            errors = _fragment_errors(
                fragment_kind, fragment, plan, registry, fragments
            )
            if errors:
                raise StoryGenerationError(
                    f"已持久化分片 {fragment_kind} 校验失败：" + "；".join(errors)
                )
        else:
            if on_stage_start:
                await on_stage_start(artifact_key)
            adjacent = _adjacent_fragment_summaries(fragment_kind, fragments, plan)
            fragment, repairs = await _generate_fragment(
                fragment_kind=fragment_kind,
                brief=brief,
                plan=plan,
                registry=registry,
                ledger=ledger,
                reference_fragments=references,
                adjacent_fragments=adjacent,
                compiled_fragments=fragments,
            )
            total_repairs += repairs
            if on_artifact:
                await on_artifact("compiling", artifact_key, fragment, repairs)
            artifacts[artifact_key] = fragment
        fragments[fragment_kind] = fragment

    raw = _assemble_canon(plan, fragments)
    raw, canon = await _repair_assembled_canon(
        raw,
        brief=brief,
        plan=plan,
        stage_label="分片汇总 Canon 未通过完整校验",
    )
    if on_artifact:
        await on_artifact("validating", "assembled_canon", raw, 0)

    review = artifacts.get("continuity_review")
    if review is None:
        if on_stage_start:
            await on_stage_start("continuity_review")
        review = await _complete_json(
            build_continuity_review_prompt(confirmed_brief=brief, canon=raw),
            stage="连贯性复核",
            role=ModelRole.STORY_CONTINUITY,
        )
        _validate_continuity_review(review)
        if on_artifact:
            await on_artifact("continuity", "continuity_review", review, 0)
    issues = [
        item
        for item in review.get("issues", [])
        if str(item.get("severity")) == "error"
    ]
    if issues:
        affected_ids = sorted(
            {
                str(act_id)
                for issue in issues
                for act_id in issue.get("affected_act_ids", [])
                if str(act_id) in {act.id for act in plan.acts}
            }
        )
        if not affected_ids:
            raise StoryGenerationError(
                "连贯性复核发现错误但未提供合法 affected_act_ids"
            )
        repair_marker = artifacts.get("continuity_repair")
        if repair_marker is not None:
            if set(repair_marker.get("affected_act_ids", [])) != set(affected_ids):
                raise StoryGenerationError("已持久化连贯性修复标记与原始问题不一致")
        else:
            if on_stage_start:
                await on_stage_start("continuity_repair")
            repaired = await _complete_json(
                build_continuity_repair_prompt(
                    confirmed_brief=brief,
                    story_plan=plan_data,
                    id_registry=registry,
                    effect_owner_ledger=ledger,
                    issues=issues,
                    act_fragments={
                        act_id: fragments[f"act:{act_id}"] for act_id in affected_ids
                    },
                ),
                stage="连贯性定向修复",
                role=ModelRole.STORY_REPAIR,
            )
            replacement = repaired.get("act_fragments")
            if not isinstance(replacement, dict) or set(replacement) != set(
                affected_ids
            ):
                raise StoryGenerationError("连贯性修复必须只返回全部受影响 Act 分片")
            for act_id, fragment in replacement.items():
                fragment = _enforce_fragment_constants(f"act:{act_id}", fragment, plan)
                fragment_errors = _fragment_errors(
                    f"act:{act_id}", fragment, plan, registry, fragments
                )
                if fragment_errors:
                    raise StoryGenerationError(
                        f"连贯性修复后的 Act «{act_id}» 非法："
                        + "；".join(fragment_errors)
                    )
                fragments[f"act:{act_id}"] = fragment
                if on_artifact:
                    await on_artifact(
                        "continuity_repair",
                        f"fragment:act:{act_id}",
                        fragment,
                        0,
                    )
            total_repairs += 1
            repair_marker = {"affected_act_ids": affected_ids}
            if on_artifact:
                await on_artifact(
                    "continuity_repair", "continuity_repair", repair_marker, 1
                )
            artifacts["continuity_repair"] = repair_marker
        raw = _assemble_canon(plan, fragments)
        raw, canon = await _repair_assembled_canon(
            raw,
            brief=brief,
            plan=plan,
            stage_label="连贯性修复后 Canon 未通过完整校验",
        )
        final_review = artifacts.get("continuity_review_final")
        if final_review is None:
            if on_stage_start:
                await on_stage_start("continuity_review_final")
            final_review = await _complete_json(
                build_continuity_review_prompt(confirmed_brief=brief, canon=raw),
                stage="修复后连贯性复核",
                role=ModelRole.STORY_CONTINUITY,
            )
            if on_artifact:
                await on_artifact(
                    "continuity", "continuity_review_final", final_review, 0
                )
        review = final_review
        _validate_continuity_review(review)
        remaining = [
            item
            for item in review.get("issues", [])
            if str(item.get("severity")) == "error"
        ]
        if remaining:
            raise StoryGenerationError("定向修复后仍有连贯性错误，任务终止")

    metrics = canon_quality_metrics(
        canon, repair_count=total_repairs, continuity_passed=True
    )
    return raw, canon, metrics


BeatOutlineBatch = PlanComplexBatch[PlanBeatOutline]
BranchBatch = PlanComplexBatch[PlanBranchBlueprint]
BeatDetailBatch = PlanComplexBatch[PlanBeatDetail]
EntityBatch = PlanSimpleBatch[PlanEntityDraft]
PlacementBatch = PlanComplexBatch[PlanBeatPlacement]
RouteBatch = PlanComplexBatch[PlanRouteText]
ClueBatch = PlanComplexBatch[PlanClueDetail]
PayoffBatch = PlanComplexBatch[PlanPayoffDetail]
EndingBatch = PlanComplexBatch[PlanEndingDetail]
OwnerBatch = PlanComplexBatch[PlanEffectOwnerChoice]

_ENTITY_CATEGORIES = (
    "actors",
    "locations",
    "encounters",
    "clues",
    "flags",
    "items",
    "actions",
)


async def _generate_story_plan_progressively(
    brief: StoryDesignBrief,
    reserved_campaign_ids: list[str],
    *,
    artifacts: dict[str, dict[str, Any]],
    on_artifact: ArtifactCallback | None,
    on_stage_start: StageStartCallback | None,
) -> tuple[StoryPlan, int]:
    """按小型封闭产物生成 StoryPlan，并在每个验证边界持久化。"""
    if brief.scale_profile is None or brief.branching_budget is None:
        raise StoryGenerationError("确认设计稿缺少 StoryPlan 规模契约")
    playable_count = brief.scale_profile.playable_beats
    branch_count = brief.branching_budget.meaningful_branch_points
    if playable_count < 3 * branch_count + 2:
        raise StoryGenerationError("确认设计稿的可玩 Beat 数无法容纳固定分支骨架")

    state = StoryPlanWorkState()
    repairs = 0

    frame, used = await _run_plan_stage(
        artifact_key="plan:frame",
        label="章节骨架",
        schema=StoryPlanFrame,
        brief=brief,
        state=state,
        target={
            "target_id": "frame",
            "act_count": brief.scale_profile.acts,
            "playable_beat_count": playable_count,
        },
        instructions=(
            "生成 campaign_id_candidate 和全部 Act 骨架。Act 数必须精确匹配目标，"
            "playable_beat_count 合计必须精确匹配目标且每个 Act 为 1–3。"
        ),
        artifacts=artifacts,
        validate=lambda value: _validate_plan_frame(
            value, brief, reserved_campaign_ids, check_reserved=True
        ),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
        reserved_campaign_ids=reserved_campaign_ids,
        resumed_validate=lambda value: _validate_plan_frame(
            value, brief, reserved_campaign_ids, check_reserved=False
        ),
    )
    state.frame = frame
    repairs += used
    other_reserved_ids = [
        value for value in reserved_campaign_ids if value != frame.campaign_id_candidate
    ]

    global_beat_offset = 0
    for act in frame.acts:
        target_ids = list(
            range(global_beat_offset, global_beat_offset + act.playable_beat_count)
        )
        key = f"plan:beat_outline:{act.id}"

        def normalize_outline_batch(
            value: BaseModel,
            *,
            offset: int = global_beat_offset,
        ) -> BaseModel:
            data = value.model_dump()
            for index, item in enumerate(data["items"], start=offset):
                if index == 0:
                    item["kind"] = "opening"
                if index == playable_count - 1:
                    item["kind"] = "climax"
            return BeatOutlineBatch.model_validate(data)

        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"Beat 大纲 {act.id}",
            schema=BeatOutlineBatch,
            brief=brief,
            state=state,
            target={
                "target_id": act.id,
                "count": act.playable_beat_count,
                "global_positions": target_ids,
            },
            instructions=(
                "为当前 Act 生成 Beat 大纲。target_id 必须等于 Act ID；items 数量必须精确匹配。"
                "代码会把全局首拍固定为 opening、末拍固定为 climax；其它拍只能是 exploration 或 conflict。"
                "estimated_minutes 要让任一路径连同结局时长落入确认时长的 ±20%，并贴近 pacing。"
            ),
            artifacts=artifacts,
            validate=lambda value, act=act: _validate_beat_outline_batch(
                value,
                target_id=act.id,
                count=act.playable_beat_count,
                existing=state.beat_outlines,
                act_ids={item.id for item in frame.acts},
                total_count=playable_count,
            ),
            normalize=normalize_outline_batch,
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        state.beat_outlines.extend(batch.items)
        repairs += used
        global_beat_offset += act.playable_beat_count

    branches, used = await _run_plan_stage(
        artifact_key="plan:branches",
        label="分支蓝图",
        schema=BranchBatch,
        brief=brief,
        state=state,
        target={"target_id": "branches", "count": branch_count},
        instructions=(
            "选择固定二选一汇流窗口并写两条不同后果。每个窗口必须占连续四拍："
            "source、紧随其后的 choice_a/choice_b、紧随其后的 reconverge；汇流点必须早于 climax。"
            "窗口不得重叠，但前一汇流点可以作为下一窗口 source。"
        ),
        artifacts=artifacts,
        validate=lambda value: _validate_branch_batch(
            value, state.beat_outlines, branch_count
        ),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
    )
    state.branch_blueprints = list(branches.items)
    repairs += used

    for batch_index, target_beats in enumerate(
        _chunks([beat.id for beat in state.beat_outlines], 3), start=1
    ):
        key = f"plan:beat_detail:{batch_index:03d}"
        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"Beat 细节 {batch_index}",
            schema=BeatDetailBatch,
            brief=brief,
            state=state,
            target={"target_id": key, "beat_ids": target_beats},
            instructions="只补充目标 Beat 的 pressure、dramatic_question、entry_hook、fail_forward。",
            artifacts=artifacts,
            validate=lambda value, key=key, ids=target_beats: _validate_targeted_batch(
                value, key, ids, "beat_id"
            ),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        state.beat_details.extend(batch.items)
        repairs += used

    budget, used = await _run_plan_stage(
        artifact_key="plan:entity_budget",
        label="数量清单",
        schema=PlanEntityBudget,
        brief=brief,
        state=state,
        target={
            "target_id": "entity_budget",
            "fixed_counts": {
                "locations": brief.scale_profile.locations,
                "encounters": brief.scale_profile.encounters,
                "clues": brief.scale_profile.clues,
            },
        },
        instructions=(
            f"只决定数量：actors 1–{2 * playable_count}；flags/items/actions 0–{playable_count}；"
            "payoffs 不能超过 flags。location/encounter/clue 数量已由确认稿固定，不要返回它们。"
        ),
        artifacts=artifacts,
        validate=lambda value: _validate_entity_budget(value, playable_count),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
        role=ModelRole.STORY_PLANNING_FAST,
    )
    state.entity_budget = budget
    repairs += used

    entity_counts = {
        "actors": budget.actors,
        "locations": brief.scale_profile.locations,
        "encounters": brief.scale_profile.encounters,
        "clues": brief.scale_profile.clues,
        "flags": budget.flags,
        "items": budget.items,
        "actions": budget.actions,
    }
    for category in _ENTITY_CATEGORIES:
        count = entity_counts[category]
        for batch_index, batch_size in enumerate(_batch_sizes(count, 5), start=1):
            key = f"plan:entities:{category}:{batch_index:03d}"
            batch, used = await _run_plan_stage(
                artifact_key=key,
                label=f"实体清单 {category} {batch_index}",
                schema=EntityBatch,
                brief=brief,
                state=state,
                target={
                    "target_id": key,
                    "category": category,
                    "count": batch_size,
                    "total_count": count,
                },
                instructions=(
                    "生成目标类别的 id/name/summary。ID 必须是全局唯一 lowercase snake_case，"
                    "不得复用 Act、Beat、固定 ending 或此前实体 ID。"
                ),
                artifacts=artifacts,
                validate=lambda value, key=key, size=batch_size: _validate_entity_batch(
                    value, key, size, state
                ),
                on_artifact=on_artifact,
                on_stage_start=on_stage_start,
            )
            getattr(state.entities, category).extend(
                PlanEntity.model_validate(item.model_dump()) for item in batch.items
            )
            repairs += used

    placement_ids = [beat.id for beat in state.beat_outlines]
    placement_chunks = _chunks(placement_ids, 3)
    for batch_index, target_beats in enumerate(placement_chunks, start=1):
        key = f"plan:placement:{batch_index:03d}"
        is_last = batch_index == len(placement_chunks)
        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"Beat 放置 {batch_index}",
            schema=PlacementBatch,
            brief=brief,
            state=state,
            target={
                "target_id": key,
                "beat_ids": target_beats,
                "final_batch_must_cover_all_entities": is_last,
            },
            instructions=(
                "为每个目标 Beat 放置至少一个 location，并选择 actor、clue、encounter 引用。"
                "每个 clue 和 encounter 在全计划中必须恰好归属一个 Beat；每个 location 和 actor 至少使用一次。"
            ),
            artifacts=artifacts,
            validate=lambda value, key=key, ids=target_beats, last=is_last: _validate_placement_batch(
                value, key, ids, state, final_batch=last
            ),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
            role=ModelRole.STORY_PLANNING_FAST,
        )
        state.placements.extend(batch.items)
        repairs += used

    route_targets = _route_targets(state.beat_outlines, state.branch_blueprints)

    async def build_route(beat_id: str) -> tuple[str, list[PlanRouteText], int]:
        targets = route_targets[beat_id]
        batch, used = await _run_plan_stage(
            artifact_key=f"plan:routes:{beat_id}",
            label=f"出口文案 {beat_id}",
            schema=RouteBatch,
            brief=brief,
            state=state,
            target={"target_id": beat_id, "ordered_to_beat_ids": targets},
            instructions=(
                "按 ordered_to_beat_ids 的原顺序各写一组 condition_summary/consequence。"
                "目标和顺序由代码锁定，响应中不得返回或改写目标 ID。"
            ),
            artifacts=artifacts,
            validate=lambda value, beat_id=beat_id, count=len(
                targets
            ): _validate_route_batch(value, beat_id, count),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        return beat_id, list(batch.items), used

    # 各 Beat 的出口文案彼此独立，并发生成以显著缩短端到端延迟。
    route_results = await asyncio.gather(
        *(build_route(beat_id) for beat_id in placement_ids)
    )
    for beat_id, items, used in route_results:
        state.routes[beat_id] = items
        repairs += used

    clue_ids = [item.id for item in state.entities.clues]
    for batch_index, target_clues in enumerate(_chunks(clue_ids, 3), start=1):
        key = f"plan:clues:{batch_index:03d}"
        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"线索细节 {batch_index}",
            schema=ClueBatch,
            brief=brief,
            state=state,
            target={"target_id": key, "clue_ids": target_clues},
            instructions="为每条目标线索填写答案、解锁内容和至少两种可执行接近方式；owner 由代码从 Beat 放置派生。",
            artifacts=artifacts,
            validate=lambda value, key=key, ids=target_clues: _validate_targeted_batch(
                value, key, ids, "clue_id"
            ),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        state.clues.extend(batch.items)
        repairs += used

    flag_ids = [item.id for item in state.entities.flags]
    for batch_index, batch_size in enumerate(_batch_sizes(budget.payoffs, 3), start=1):
        key = f"plan:payoffs:{batch_index:03d}"
        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"伏笔回收 {batch_index}",
            schema=PayoffBatch,
            brief=brief,
            state=state,
            target={
                "target_id": key,
                "count": batch_size,
                "available_flag_ids": flag_ids,
            },
            instructions=(
                "选择尚未使用的 flag，填写铺垫 Beat、较晚的可玩回收 Beat和描述。"
                "每个 flag 最多一条；payoff_flag_ids 由代码派生。"
            ),
            artifacts=artifacts,
            validate=lambda value, key=key, size=batch_size: _validate_payoff_batch(
                value, key, size, state
            ),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
        )
        state.payoffs.extend(batch.items)
        repairs += used

    effect_ids = [item.id for item in state.entities.flags + state.entities.items]
    endings, used = await _run_plan_stage(
        artifact_key="plan:endings",
        label="双结局",
        schema=EndingBatch,
        brief=brief,
        state=state,
        target={"target_id": "endings", "ending_ids": ["ending_win", "ending_lose"]},
        instructions=(
            "精确填写 ending_win 与 ending_lose 两项的结局目标、required_facts 和 payoffs。"
            "结局 ID、顺序与 outcome 由代码锁定；事实和效果只能引用已有 flag/item ID。"
        ),
        artifacts=artifacts,
        validate=lambda value: _validate_ending_batch(value, effect_ids, flag_ids),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
    )
    state.endings = list(endings.items)
    repairs += used

    owner_targets = [(item.id, "flag") for item in state.entities.flags] + [
        (item.id, "item") for item in state.entities.items
    ]
    owner_chunks = _chunks(owner_targets, 3)
    for batch_index, targets in enumerate(owner_chunks, start=1):
        key = f"plan:owners:{batch_index:03d}"
        batch, used = await _run_plan_stage(
            artifact_key=key,
            label=f"效果 owner {batch_index}",
            schema=OwnerBatch,
            brief=brief,
            state=state,
            target={
                "target_id": key,
                "effects": [
                    {"effect_id": effect_id, "effect_kind": kind}
                    for effect_id, kind in targets
                ],
            },
            instructions=(
                "为每个目标效果选择唯一 owner_kind/owner_id。effect_kind 由代码根据目标类别派生，"
                "响应不得返回它；owner_id 必须符合 owner_kind 的 ID 类别。"
            ),
            artifacts=artifacts,
            validate=lambda value, key=key, targets=targets: _validate_owner_batch(
                value, key, targets, state
            ),
            on_artifact=on_artifact,
            on_stage_start=on_stage_start,
            role=ModelRole.STORY_PLANNING_FAST,
        )
        state.owners.extend(batch.items)
        repairs += used

    candidate = _compile_story_plan(brief, state)
    if on_stage_start:
        await on_stage_start("plan")
    plan, final_repairs = await _finalize_progressive_plan(
        candidate,
        brief,
        other_reserved_ids,
    )
    return plan, repairs + final_repairs


async def _run_plan_stage(
    *,
    artifact_key: str,
    label: str,
    schema: type[BaseModel],
    brief: StoryDesignBrief,
    state: StoryPlanWorkState,
    target: dict[str, Any],
    instructions: str,
    artifacts: dict[str, dict[str, Any]],
    validate: Callable[[Any], list[str]],
    on_artifact: ArtifactCallback | None,
    on_stage_start: StageStartCallback | None,
    normalize: Callable[[BaseModel], BaseModel] | None = None,
    reserved_campaign_ids: list[str] | None = None,
    resumed_validate: Callable[[Any], list[str]] | None = None,
    role: ModelRole = ModelRole.STORY_PLANNING,
) -> tuple[Any, int]:
    """加载或生成一个小阶段；一次初稿加至多一次定向修复。

    ``role`` 允许把纯机械的小阶段切到 fast 模型，降低端到端延迟；修复仍走
    ``STORY_REPAIR`` 职责。
    """
    if artifact_key in artifacts:
        try:
            value = schema.model_validate(artifacts[artifact_key])
            if normalize:
                value = normalize(value)
        except ValidationError as exc:
            raise StoryGenerationError(
                f"已持久化规划产物 {artifact_key} 字段不合法：{exc}"
            ) from exc
        errors = (resumed_validate or validate)(value)
        if errors:
            raise StoryGenerationError(
                f"已持久化规划产物 {artifact_key} 校验失败：" + "；".join(errors)
            )
        return value, 0

    if on_stage_start:
        await on_stage_start(artifact_key)
    prompt = build_story_plan_stage_prompt(
        confirmed_brief=brief,
        current_target=target,
        response_schema=schema,
        generated_story_so_far=state,
        instructions=instructions,
        reserved_campaign_ids=reserved_campaign_ids,
    )
    initial_errors: list[str] | None = None
    try:
        raw = await _complete_json(
            prompt,
            stage=f"计划 {label}",
            role=role,
        )
    except StoryGenerationError as exc:
        if "输出不是可解析的 JSON 对象" not in str(exc):
            raise
        raw = {}
        initial_errors = [str(exc)]
    for attempt in range(2):
        errors: list[str]
        try:
            value = schema.model_validate(raw) if initial_errors is None else None
            if value is not None and normalize:
                value = normalize(value)
            errors = validate(value) if value is not None else initial_errors or []
        except ValidationError as exc:
            value = None
            errors = _story_interview_validation_errors(exc)
        if value is not None and not errors:
            payload = value.model_dump(mode="json")
            if on_artifact:
                await on_artifact("planning", artifact_key, payload, attempt)
            artifacts[artifact_key] = payload
            return value, attempt
        if attempt == 1:
            raise StoryGenerationError(
                f"规划小阶段 {label} 修复后仍不合法：" + "；".join(errors)
            )
        repair_prompt = build_story_plan_stage_repair_prompt(
            confirmed_brief=brief,
            current_target=target,
            response_schema=schema,
            generated_story_so_far=state,
            instructions=instructions,
            invalid_candidate=raw,
            validation_errors=errors,
            reserved_campaign_ids=reserved_campaign_ids,
        )
        _log_repair_attempt(
            stage=f"规划小阶段 {label}",
            repair_round=1,
            errors=errors,
            prompt=repair_prompt,
            max_attempts=1,
        )
        raw = await _complete_json(
            repair_prompt,
            stage=f"计划 {label} 定向修复",
            role=ModelRole.STORY_REPAIR,
        )
        initial_errors = None
    raise AssertionError("规划小阶段修复循环未按预期结束")


def _validate_plan_frame(
    frame: StoryPlanFrame,
    brief: StoryDesignBrief,
    reserved_campaign_ids: list[str],
    *,
    check_reserved: bool,
) -> list[str]:
    profile = brief.scale_profile
    if profile is None:
        return ["确认稿缺少 scale_profile"]
    errors: list[str] = []
    if len(frame.acts) != profile.acts:
        errors.append(f"Act 数必须等于 {profile.acts}")
    if sum(act.playable_beat_count for act in frame.acts) != profile.playable_beats:
        errors.append(f"可玩 Beat 总数必须等于 {profile.playable_beats}")
    if any(act.playable_beat_count > 3 for act in frame.acts):
        errors.append("每个 Act 的 Beat 大纲批次最多 3 项")
    act_ids = [act.id for act in frame.acts]
    if len(act_ids) != len(set(act_ids)):
        errors.append("Act ID 必须全局唯一")
    if frame.campaign_id_candidate in act_ids:
        errors.append("campaign ID 不得与 Act ID 重复")
    if {"ending_win", "ending_lose"} & set(act_ids):
        errors.append("Act ID 不得占用固定 ending ID")
    if check_reserved and frame.campaign_id_candidate in reserved_campaign_ids:
        errors.append("campaign_id_candidate 已被占用")
    return errors


def _validate_beat_outline_batch(
    batch: PlanComplexBatch[PlanBeatOutline],
    *,
    target_id: str,
    count: int,
    existing: list[PlanBeatOutline],
    act_ids: set[str],
    total_count: int,
) -> list[str]:
    errors = _validate_batch_header(batch, target_id, count)
    combined = [*existing, *batch.items]
    ids = [item.id for item in combined]
    if len(ids) != len(set(ids)) or set(ids) & act_ids:
        errors.append("Beat ID 必须全局唯一且不得与 Act ID 重复")
    if {"ending_win", "ending_lose"} & set(ids):
        errors.append("可玩 Beat 不得占用固定 ending ID")
    start = len(existing)
    for offset, beat in enumerate(batch.items, start=start):
        if offset not in {0, total_count - 1} and beat.kind in {"opening", "climax"}:
            errors.append(f"内部 Beat «{beat.id}» 只能是 exploration 或 conflict")
    return errors


def _validate_branch_batch(
    batch: PlanComplexBatch[PlanBranchBlueprint],
    outlines: list[PlanBeatOutline],
    count: int,
) -> list[str]:
    errors = _validate_batch_header(batch, "branches", count)
    positions = {beat.id: index for index, beat in enumerate(outlines)}
    windows: list[tuple[int, int]] = []
    for branch in batch.items:
        source = positions.get(branch.source_beat_id)
        choices = [positions.get(value) for value in branch.choice_beat_ids]
        reconverge = positions.get(branch.reconverge_at)
        if (
            source is None
            or any(value is None for value in choices)
            or reconverge is None
        ):
            errors.append("分支蓝图只能引用已有可玩 Beat")
            continue
        if choices != [source + 1, source + 2] or reconverge != source + 3:
            errors.append(f"分支 «{branch.source_beat_id}» 必须使用连续四拍窗口")
            continue
        if reconverge >= len(outlines) - 1:
            errors.append(f"分支 «{branch.source_beat_id}» 必须在 climax 前汇流")
        windows.append((source, reconverge))
    windows.sort()
    for previous, current in zip(windows, windows[1:]):
        if current[0] < previous[1]:
            errors.append("分支窗口不得重叠，只有相邻窗口可共享汇流/source")
    if len({branch.source_beat_id for branch in batch.items}) != len(batch.items):
        errors.append("分支 source 不得重复")
    if any(len(set(branch.distinct_consequences)) != 2 for branch in batch.items):
        errors.append("每个分支必须提供两条不同后果")
    return errors


def _validate_entity_budget(budget: PlanEntityBudget, playable_count: int) -> list[str]:
    errors: list[str] = []
    if not 1 <= budget.actors <= 2 * playable_count:
        errors.append(f"actors 必须在 1–{2 * playable_count} 之间")
    for name in ("flags", "items", "actions"):
        if getattr(budget, name) > playable_count:
            errors.append(f"{name} 不能超过可玩 Beat 数 {playable_count}")
    if budget.payoffs > budget.flags:
        errors.append("payoffs 不能超过 flags 数量")
    return errors


def _validate_entity_batch(
    batch: PlanSimpleBatch[PlanEntityDraft],
    target_id: str,
    count: int,
    state: StoryPlanWorkState,
) -> list[str]:
    errors = _validate_batch_header(batch, target_id, count)
    existing = _work_state_global_ids(state)
    ids = [item.id for item in batch.items]
    duplicates = {value for value in ids if ids.count(value) > 1}
    conflicts = set(ids) & existing
    if duplicates or conflicts:
        errors.append("实体 ID 必须在全部 Act、Beat、ending 和实体类别中全局唯一")
    return errors


def _validate_placement_batch(
    batch: PlanComplexBatch[PlanBeatPlacement],
    target_id: str,
    target_beats: list[str],
    state: StoryPlanWorkState,
    *,
    final_batch: bool,
) -> list[str]:
    errors = _validate_targeted_batch(batch, target_id, target_beats, "beat_id")
    allowed = {
        category: {item.id for item in getattr(state.entities, category)}
        for category in ("locations", "actors", "clues", "encounters")
    }
    seen_clues = [value for item in state.placements for value in item.clue_ids]
    seen_encounters = [
        item.encounter_id for item in state.placements if item.encounter_id is not None
    ]
    for placement in batch.items:
        for field, category in (
            ("location_ids", "locations"),
            ("actor_ids", "actors"),
            ("clue_ids", "clues"),
        ):
            values = getattr(placement, field)
            if len(values) != len(set(values)):
                errors.append(f"Beat «{placement.beat_id}» 的 {field} 不得重复")
            unknown = set(values) - allowed[category]
            if unknown:
                errors.append(
                    f"Beat «{placement.beat_id}» 引用了未知 {category}: {sorted(unknown)}"
                )
        if (
            placement.encounter_id
            and placement.encounter_id not in allowed["encounters"]
        ):
            errors.append(f"Beat «{placement.beat_id}» 引用了未知 encounter")
        seen_clues.extend(placement.clue_ids)
        if placement.encounter_id:
            seen_encounters.append(placement.encounter_id)
    if len(seen_clues) != len(set(seen_clues)):
        errors.append("每个 clue 必须且只能放置到一个 Beat")
    if len(seen_encounters) != len(set(seen_encounters)):
        errors.append("每个 encounter 必须且只能放置到一个 Beat")
    if final_batch:
        combined = [*state.placements, *batch.items]
        used = {
            "locations": {value for item in combined for value in item.location_ids},
            "actors": {value for item in combined for value in item.actor_ids},
            "clues": {value for item in combined for value in item.clue_ids},
            "encounters": {
                item.encounter_id for item in combined if item.encounter_id is not None
            },
        }
        for category in ("locations", "actors"):
            if used[category] != allowed[category]:
                errors.append(f"全部 {category} 必须至少被一个 Beat 使用")
        for category in ("clues", "encounters"):
            if used[category] != allowed[category]:
                errors.append(f"全部 {category} 必须恰好归属一个 Beat")
    return errors


def _validate_route_batch(
    batch: PlanComplexBatch[PlanRouteText], target_id: str, count: int
) -> list[str]:
    return _validate_batch_header(batch, target_id, count)


def _validate_payoff_batch(
    batch: PlanComplexBatch[PlanPayoffDetail],
    target_id: str,
    count: int,
    state: StoryPlanWorkState,
) -> list[str]:
    errors = _validate_batch_header(batch, target_id, count)
    flags = {item.id for item in state.entities.flags}
    positions = {item.id: index for index, item in enumerate(state.beat_outlines)}
    used = {item.flag_id for item in state.payoffs}
    for payoff in batch.items:
        if payoff.flag_id not in flags or payoff.flag_id in used:
            errors.append(f"payoff flag «{payoff.flag_id}» 必须存在且只能使用一次")
        used.add(payoff.flag_id)
        setup = positions.get(payoff.setup_beat_id)
        target = positions.get(payoff.payoff_beat_id)
        if setup is None or target is None or setup >= target:
            errors.append(
                f"payoff «{payoff.flag_id}» 必须从较早可玩 Beat 回收到较晚可玩 Beat"
            )
    return errors


def _validate_ending_batch(
    batch: PlanComplexBatch[PlanEndingDetail],
    effect_ids: list[str],
    flag_ids: list[str],
) -> list[str]:
    errors = _validate_targeted_batch(
        batch, "endings", ["ending_win", "ending_lose"], "ending_id"
    )
    effects = set(effect_ids)
    flags = set(flag_ids)
    for ending in batch.items:
        if set(ending.required_facts) - flags:
            errors.append(f"结局 «{ending.ending_id}» 的 required_facts 只能引用 flag")
        if set(ending.payoffs) - effects:
            errors.append(f"结局 «{ending.ending_id}» 的 payoffs 只能引用 flag/item")
    return errors


def _validate_owner_batch(
    batch: PlanComplexBatch[PlanEffectOwnerChoice],
    target_id: str,
    targets: list[tuple[str, str]],
    state: StoryPlanWorkState,
) -> list[str]:
    errors = _validate_targeted_batch(
        batch, target_id, [effect_id for effect_id, _ in targets], "effect_id"
    )
    allowed = {
        "discovery": {item.id for item in state.entities.clues},
        "encounter_win": {item.id for item in state.entities.encounters},
        "initial_state": {item.id for item in state.beat_outlines},
        "rule_action": {item.id for item in state.entities.actions},
        "dm_free_write": {item.id for item in state.entities.flags},
    }
    for owner in batch.items:
        if owner.owner_id not in allowed[owner.owner_kind]:
            errors.append(
                f"效果 «{owner.effect_id}» 的 owner_id 与 owner_kind={owner.owner_kind} 不匹配"
            )
    return errors


def _validate_targeted_batch(
    batch: PlanBatch[Any], target_id: str, ids: list[str], field: str
) -> list[str]:
    errors = _validate_batch_header(batch, target_id, len(ids))
    actual = [getattr(item, field) for item in batch.items]
    if actual != ids:
        errors.append(f"{field} 必须按目标顺序精确返回 {ids}")
    return errors


def _validate_batch_header(
    batch: PlanBatch[Any], target_id: str, count: int
) -> list[str]:
    errors: list[str] = []
    if batch.target_id != target_id:
        errors.append(f"target_id 必须等于 {target_id}")
    if len(batch.items) != count:
        errors.append(f"items 数量必须等于 {count}")
    return errors


def _route_targets(
    outlines: list[PlanBeatOutline], branches: list[PlanBranchBlueprint]
) -> dict[str, list[str]]:
    targets = {
        beat.id: (
            [outlines[index + 1].id] if index + 1 < len(outlines) else ["ending_win"]
        )
        for index, beat in enumerate(outlines)
    }
    for branch in branches:
        targets[branch.source_beat_id] = list(branch.choice_beat_ids)
        targets[branch.choice_beat_ids[0]] = [branch.reconverge_at]
        targets[branch.choice_beat_ids[1]] = [branch.reconverge_at]
    return targets


def _compile_story_plan(
    brief: StoryDesignBrief, state: StoryPlanWorkState
) -> dict[str, Any]:
    """把已验证小产物编译为现有 StoryPlan wire format。"""
    if (
        state.frame is None
        or state.entity_budget is None
        or brief.scale_profile is None
    ):
        raise StoryGenerationError("StoryPlan 渐进状态不完整")
    details = {item.beat_id: item for item in state.beat_details}
    placements = {item.beat_id: item for item in state.placements}
    routes = _route_targets(state.beat_outlines, state.branch_blueprints)
    payoff_flags: dict[str, list[str]] = {}
    for payoff in state.payoffs:
        payoff_flags.setdefault(payoff.payoff_beat_id, []).append(payoff.flag_id)

    beat_act: dict[str, str] = {}
    cursor = 0
    for act in state.frame.acts:
        for beat in state.beat_outlines[cursor : cursor + act.playable_beat_count]:
            beat_act[beat.id] = act.id
        cursor += act.playable_beat_count

    beats: list[dict[str, Any]] = []
    for outline in state.beat_outlines:
        detail = details[outline.id]
        placement = placements[outline.id]
        beats.append(
            {
                **outline.model_dump(),
                "act_id": beat_act[outline.id],
                **detail.model_dump(exclude={"beat_id"}),
                **placement.model_dump(exclude={"beat_id"}),
                "exits": [
                    {
                        "to_beat_id": target,
                        **text.model_dump(),
                    }
                    for target, text in zip(
                        routes[outline.id], state.routes[outline.id]
                    )
                ],
                "payoff_flag_ids": payoff_flags.get(outline.id, []),
            }
        )

    ending_minutes = max(
        1,
        round(
            int(brief.duration_minutes or 20)
            * int(brief.pacing.ending_percent if brief.pacing else 5)
            / 100
        ),
    )
    climax = beats[-1]
    ending_by_id = {item.ending_id: item for item in state.endings}
    ending_act_id = state.frame.acts[-1].id
    for ending_id in ("ending_win", "ending_lose"):
        ending = ending_by_id[ending_id]
        beats.append(
            {
                "id": ending_id,
                "act_id": ending_act_id,
                "kind": "ending",
                "estimated_minutes": ending_minutes,
                "objective": ending.objective,
                "pressure": "结局已定",
                "dramatic_question": "",
                "entry_hook": "",
                "location_ids": list(climax["location_ids"]),
                "actor_ids": [],
                "clue_ids": [],
                "encounter_id": None,
                "exits": [],
                "fail_forward": "",
                "payoff_flag_ids": [],
            }
        )

    beat_by_act: dict[str, list[dict[str, Any]]] = {}
    for beat in beats:
        beat_by_act.setdefault(beat["act_id"], []).append(beat)
    acts = [
        {
            "id": act.id,
            "purpose": act.purpose,
            "estimated_minutes": sum(
                beat["estimated_minutes"] for beat in beat_by_act.get(act.id, [])
            ),
            "beat_ids": [beat["id"] for beat in beat_by_act.get(act.id, [])],
            "turning_point": act.turning_point,
        }
        for act in state.frame.acts
    ]
    clue_owner = {
        clue_id: placement.beat_id
        for placement in state.placements
        for clue_id in placement.clue_ids
    }
    effect_kind = {
        **{item.id: "flag" for item in state.entities.flags},
        **{item.id: "item" for item in state.entities.items},
    }
    return {
        "plan_version": 1,
        "campaign_id_candidate": state.frame.campaign_id_candidate,
        "start_beat_id": state.beat_outlines[0].id,
        "scale_profile": brief.scale_profile.model_dump(),
        "acts": acts,
        "beats": beats,
        "entities": state.entities.model_dump(),
        "clue_graph": [
            {
                **clue.model_dump(),
                "acquisition_owner": clue_owner[clue.clue_id],
            }
            for clue in state.clues
        ],
        "branch_points": [
            {
                "beat_id": branch.source_beat_id,
                "choices": list(branch.choice_beat_ids),
                "distinct_consequences": list(branch.distinct_consequences),
                "reconverge_at": branch.reconverge_at,
            }
            for branch in state.branch_blueprints
        ],
        "foreshadowing_payoffs": [item.model_dump() for item in state.payoffs],
        "ending_routes": [
            {
                "ending_id": ending.ending_id,
                "outcome": "win" if ending.ending_id == "ending_win" else "lose",
                "required_facts": list(ending.required_facts),
                "payoffs": list(ending.payoffs),
            }
            for ending in state.endings
        ],
        "effect_owner_ledger": [
            {
                **owner.model_dump(),
                "effect_kind": effect_kind[owner.effect_id],
            }
            for owner in state.owners
        ],
    }


async def _finalize_progressive_plan(
    candidate: dict[str, Any],
    brief: StoryDesignBrief,
    reserved_campaign_ids: list[str],
) -> tuple[StoryPlan, int]:
    """完整验收只允许一次跨阶段修复，并锁住已预留 ID 与固定拓扑。"""
    plan, issues, normalized = _validate_story_plan_candidate(candidate, brief)
    fixed_errors = _fixed_topology_errors(plan, brief) if plan is not None else []
    if plan is not None and not issues and not fixed_errors:
        return plan, 0

    campaign_id = str(candidate.get("campaign_id_candidate") or "")
    structural = bool(fixed_errors) or any(
        issue.category == "structural" for issue in issues
    )
    errors = [issue.message for issue in issues] + fixed_errors
    if structural:
        repair_prompt = build_story_plan_replan_prompt(
            candidate=normalized,
            confirmed_brief=brief,
            issues=issues,
            reserved_campaign_ids=reserved_campaign_ids,
        ) + (
            "\n额外固定契约：campaign_id_candidate 必须保持为 "
            f"{campaign_id}；分支仍须是连续四拍的固定二选一汇流窗口；climax 只能指向 ending_win。"
        )
    else:
        sections = affected_story_plan_sections(issues)
        repair_prompt = build_story_plan_repair_prompt(
            candidate=normalized,
            confirmed_brief=brief,
            issues=issues,
            affected_sections=sections,
        )
    _log_repair_attempt(
        stage="StoryPlan 最终验收",
        repair_round=1,
        errors=errors,
        prompt=repair_prompt,
        max_attempts=1,
    )
    repair = await _complete_json(
        repair_prompt,
        stage="计划最终验收修复",
        role=ModelRole.STORY_REPAIR,
    )
    if structural:
        repaired = repair
    else:
        try:
            repaired = merge_story_plan_sections(
                previous=normalized,
                repair=repair,
                allowed_sections=affected_story_plan_sections(issues),
            )
        except ValueError as exc:
            raise StoryGenerationError(str(exc)) from exc
    if repaired.get("campaign_id_candidate") != campaign_id:
        raise StoryGenerationError("StoryPlan 最终修复不得改变已预留 campaign_id")
    final_plan, final_issues, _ = _validate_story_plan_candidate(repaired, brief)
    final_fixed = (
        _fixed_topology_errors(final_plan, brief) if final_plan is not None else []
    )
    if final_plan is None or final_issues or final_fixed:
        raise StoryGenerationError(
            "StoryPlan 最终修复后仍不合法："
            + "；".join([item.message for item in final_issues] + final_fixed)
        )
    return final_plan, 1


def _fixed_topology_errors(plan: StoryPlan, brief: StoryDesignBrief) -> list[str]:
    playable = [beat for beat in plan.beats if beat.kind != "ending"]
    endings = [beat for beat in plan.beats if beat.kind == "ending"]
    errors: list[str] = []
    if not playable:
        return ["固定分支骨架缺少可玩 Beat"]
    if plan.start_beat_id != playable[0].id:
        errors.append("固定分支骨架必须从顺序首拍开始")
    if playable[0].kind != "opening" or playable[-1].kind != "climax":
        errors.append("固定分支骨架必须首拍 opening、末拍 climax")
    if any(beat.kind in {"opening", "climax"} for beat in playable[1:-1]):
        errors.append("固定分支骨架的内部 Beat 不能是 opening/climax")
    if {beat.id for beat in endings} != {"ending_win", "ending_lose"}:
        errors.append("固定分支骨架必须使用 ending_win 与 ending_lose")
    try:
        blueprints = [
            PlanBranchBlueprint(
                source_beat_id=item.beat_id,
                choice_beat_ids=item.choices,
                reconverge_at=item.reconverge_at,
                distinct_consequences=item.distinct_consequences,
            )
            for item in plan.branch_points
        ]
        branch_batch = BranchBatch(target_id="branches", items=blueprints)
    except ValidationError:
        errors.append("分支必须是至多三组严格二选一蓝图")
        return errors
    branch_errors = _validate_branch_batch(
        branch_batch,
        [
            PlanBeatOutline(
                id=beat.id,
                kind=beat.kind,
                estimated_minutes=beat.estimated_minutes,
                objective=beat.objective,
            )
            for beat in playable
        ],
        (
            brief.branching_budget.meaningful_branch_points
            if brief.branching_budget
            else 0
        ),
    )
    errors.extend(branch_errors)
    targets = _route_targets(
        [
            PlanBeatOutline(
                id=beat.id,
                kind=beat.kind,
                estimated_minutes=beat.estimated_minutes,
                objective=beat.objective,
            )
            for beat in playable
        ],
        blueprints,
    )
    for beat in playable:
        if [exit_.to_beat_id for exit_ in beat.exits] != targets[beat.id]:
            errors.append(f"Beat «{beat.id}» 的出口目标不符合固定分支骨架")
    return errors


def _work_state_global_ids(state: StoryPlanWorkState) -> set[str]:
    ids = {"ending_win", "ending_lose"}
    if state.frame:
        ids.update(act.id for act in state.frame.acts)
    ids.update(beat.id for beat in state.beat_outlines)
    for category in _ENTITY_CATEGORIES:
        ids.update(item.id for item in getattr(state.entities, category))
    return ids


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _batch_sizes(total: int, size: int) -> list[int]:
    return [len(batch) for batch in _chunks(list(range(total)), size)]


async def _generate_story_plan(
    brief: StoryDesignBrief, reserved_campaign_ids: list[str]
) -> tuple[StoryPlan, int]:
    """生成并归一化 StoryPlan，按问题类别执行有界修复。"""
    raw = await _complete_json(
        build_story_plan_prompt(brief, reserved_campaign_ids=reserved_campaign_ids)
        + "\n生成前额外自检：branch_points.choices 必须是源 Beat 的不同出口 Beat ID，"
        "不是选择文案；剧情分支须在高潮前汇流，最终胜负结局不作为 meaningful branch；"
        "effect_owner_ledger 的 owner_kind 与 owner_id 必须遵守 schema 中的 ID 类别配对。",
        stage="计划",
        role=ModelRole.STORY_PLANNING,
    )
    previous_fingerprint: tuple[tuple[str, tuple[str | int, ...]], ...] | None = None
    local_repairs = 0
    replans = 0

    while True:
        plan, issues, raw = _validate_story_plan_candidate(raw, brief)
        if plan is not None and not issues:
            return plan, local_repairs + replans

        fingerprint = story_plan_issue_fingerprint(issues)
        if fingerprint == previous_fingerprint:
            raise StoryGenerationError(
                "StoryPlan 校验问题连续两轮未变化，已停止修复："
                + "；".join(issue.message for issue in issues)
            )
        previous_fingerprint = fingerprint
        errors = [issue.message for issue in issues]

        if any(issue.category == "structural" for issue in issues):
            if replans >= MAX_STORY_PLAN_REPLANS:
                raise StoryGenerationError(
                    "StoryPlan 结构重规划后仍不合法：" + "；".join(errors)
                )
            repair_prompt = build_story_plan_replan_prompt(
                candidate=raw,
                confirmed_brief=brief,
                issues=issues,
                reserved_campaign_ids=reserved_campaign_ids,
            )
            _log_repair_attempt(
                stage="StoryPlan 结构重规划",
                repair_round=replans + 1,
                errors=errors,
                prompt=repair_prompt,
                max_attempts=MAX_STORY_PLAN_REPLANS,
            )
            raw = await _complete_json(
                repair_prompt,
                stage="计划结构重规划",
                role=ModelRole.STORY_REPAIR,
            )
            replans += 1
            continue

        if local_repairs >= MAX_STORY_PLAN_LOCAL_REPAIRS:
            raise StoryGenerationError(
                "StoryPlan 局部修复预算耗尽：" + "；".join(errors)
            )
        sections = affected_story_plan_sections(issues)
        repair_prompt = build_story_plan_repair_prompt(
            candidate=raw,
            confirmed_brief=brief,
            issues=issues,
            affected_sections=sections,
        )
        _log_repair_attempt(
            stage="StoryPlan 局部修复",
            repair_round=local_repairs + 1,
            errors=errors,
            prompt=repair_prompt,
            max_attempts=MAX_STORY_PLAN_LOCAL_REPAIRS,
        )
        repair = await _complete_json(
            repair_prompt,
            stage=f"计划局部修复（第 {local_repairs + 1} 次）",
            role=ModelRole.STORY_REPAIR,
        )
        try:
            raw = merge_story_plan_sections(
                previous=raw,
                repair=repair,
                allowed_sections=sections,
            )
        except ValueError as exc:
            raise StoryGenerationError(str(exc)) from exc
        local_repairs += 1


def _validate_story_plan_candidate(
    raw: dict[str, Any], brief: StoryDesignBrief
) -> tuple[StoryPlan | None, list[PlanValidationIssue], dict[str, Any]]:
    """每轮候选都先归一化，再执行完整 schema 与确定性校验。"""
    try:
        normalized = normalize_story_plan_candidate(raw, brief)
    except ValidationError as exc:
        return None, story_plan_field_issues(exc), raw
    except ValueError as exc:
        issue = PlanValidationIssue(
            code="normalization_failed",
            path=("scale_profile",),
            category="structural",
            affected_sections=frozenset({"scale_profile"}),
            message=f"StoryPlan 归一化失败：{exc}",
        )
        return None, [issue], raw

    changes = story_plan_normalization_changes(raw, normalized)
    if changes:
        logger.info(
            "[story_generator] StoryPlan 已完成确定性归一化 | 变更区段=%s",
            "、".join(changes),
        )
    try:
        plan = StoryPlan.model_validate(normalized)
    except ValidationError as exc:
        return None, story_plan_field_issues(exc), normalized
    return plan, _story_plan_issues(plan, brief), normalized


def _enforce_fragment_constants(
    fragment_kind: str,
    fragment: dict[str, Any],
    plan: StoryPlan,
) -> dict[str, Any]:
    """把可由 StoryPlan 确定性推导的机械字段回填进分片，再交给确定性校验。

    模型只需写叙事内容（objective/pressure、出口文案、线索正文、遭遇细节等），
    而 id、act_id、kind、estimated_minutes、location_ids、exits 与 Trigger ID 这些
    「必须逐字符与计划一致」的字段由代码强制生成，从源头消除最脆弱的一类校验失败。
    """
    if fragment_kind == "top_level":
        fragment = dict(fragment)
        fragment["campaign_id"] = plan.campaign_id_candidate
        fragment["start_beat_id"] = plan.start_beat_id
        fragment["runtime_location_scoping"] = True
        fragment["declared_flags"] = sorted(item.id for item in plan.entities.flags)
        return fragment

    if not (fragment_kind.startswith("act:") or fragment_kind == "endings"):
        return fragment

    plan_beats = {beat.id: beat for beat in plan.beats}
    normalized_beats: list[dict[str, Any]] = []
    for raw_beat in fragment.get("beats", []):
        if not isinstance(raw_beat, dict):
            normalized_beats.append(raw_beat)
            continue
        beat = dict(raw_beat)
        beat_id = str(beat.get("id", ""))
        planned = plan_beats.get(beat_id)
        if planned is None:
            normalized_beats.append(beat)
            continue
        beat["act_id"] = planned.act_id
        beat["kind"] = planned.kind
        beat["estimated_minutes"] = planned.estimated_minutes
        beat["location_ids"] = list(planned.location_ids)
        # 出口与推进条件 Trigger ID 完全由计划推导，不信任模型逐字符复写。
        beat["exits"] = [
            {
                "trigger_id": f"trigger_{beat_id}_{index + 1}",
                "next_beat_id": exit_.to_beat_id,
            }
            for index, exit_ in enumerate(planned.exits)
        ]
        conditions = beat.get("advance_conditions")
        if isinstance(conditions, list):
            for index, trigger in enumerate(conditions):
                if isinstance(trigger, dict):
                    trigger["id"] = f"trigger_{beat_id}_{index + 1}"
        encounter = beat.get("encounter")
        if isinstance(encounter, dict) and planned.encounter_id:
            encounter["id"] = planned.encounter_id
        # 线索正文保留模型创作，只把 id 与顺序对齐到计划的 clue_ids。
        clues = beat.get("key_info")
        if isinstance(clues, list):
            planned_clue_ids = list(planned.clue_ids)
            by_id = {
                str(item.get("id")): item
                for item in clues
                if isinstance(item, dict)
            }
            if set(by_id) == set(planned_clue_ids):
                beat["key_info"] = [
                    {**by_id[clue_id], "id": clue_id}
                    for clue_id in planned_clue_ids
                    if clue_id in by_id
                ]
        normalized_beats.append(beat)
    fragment["beats"] = normalized_beats
    return fragment


async def _generate_fragment(
    *,
    fragment_kind: str,
    brief: StoryDesignBrief,
    plan: StoryPlan,
    registry: dict[str, list[str]],
    ledger: list[dict[str, Any]],
    reference_fragments: list[dict[str, Any]],
    adjacent_fragments: list[dict[str, Any]],
    compiled_fragments: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    raw = await _complete_json(
        build_fragment_prompt(
            fragment_kind=fragment_kind,
            confirmed_brief=brief,
            story_plan=plan.model_dump(),
            id_registry=registry,
            effect_owner_ledger=ledger,
            reference_fragments=reference_fragments,
            adjacent_fragments=adjacent_fragments,
        ),
        stage=f"分片 {fragment_kind}",
        role=ModelRole.STORY_AUTHORING,
    )
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        raw = _enforce_fragment_constants(fragment_kind, raw, plan)
        errors = _fragment_errors(
            fragment_kind, raw, plan, registry, compiled_fragments
        )
        if not errors:
            return raw, attempt
        if attempt == MAX_REPAIR_ATTEMPTS:
            raise StoryGenerationError(
                f"分片 {fragment_kind} 在两次修复后仍不合法：" + "；".join(errors)
            )
        raw = await _complete_json(
            build_fragment_repair_prompt(
                fragment_kind=fragment_kind,
                fragment=raw,
                validation_errors=errors,
                confirmed_brief=brief,
                story_plan=plan.model_dump(),
                id_registry=registry,
                effect_owner_ledger=ledger,
            ),
            stage=f"分片 {fragment_kind} 修复（第 {attempt + 1} 次）",
            role=ModelRole.STORY_REPAIR,
        )
    raise AssertionError("分片修复循环未按预期结束")


def _fragment_errors(
    fragment_kind: str,
    fragment: dict[str, Any],
    plan: StoryPlan,
    registry: dict[str, list[str]],
    compiled_fragments: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    errors = [
        *validate_fragment_ids(fragment_kind, fragment, registry),
        *validate_fragment_runtime(fragment_kind, fragment, plan, compiled_fragments),
    ]
    allowed_keys = {
        "cast": {"cast"},
        "locations": {"locations"},
        "actions": {"action_definitions"},
        "endings": {"beats"},
    }
    if fragment_kind.startswith("act:"):
        allowed = {"beats"}
    else:
        allowed = allowed_keys.get(fragment_kind)
    if allowed is not None and set(fragment) != allowed:
        errors.append(f"分片 {fragment_kind} 顶层字段必须精确为 {sorted(allowed)}")
    expected: set[str] = set()
    actual: set[str] = set()
    key = ""
    if fragment_kind == "cast":
        key, expected = "cast", set(registry["actors"])
    elif fragment_kind == "locations":
        key, expected = "locations", set(registry["locations"])
    elif fragment_kind == "actions":
        key, expected = "action_definitions", set(registry["actions"])
    elif fragment_kind.startswith("act:"):
        key = "beats"
        act_id = fragment_kind.partition(":")[2]
        expected = {
            beat.id
            for beat in plan.beats
            if beat.act_id == act_id and beat.kind != "ending"
        }
    elif fragment_kind == "endings":
        key = "beats"
        expected = {beat.id for beat in plan.beats if beat.kind == "ending"}
    elif fragment_kind == "top_level":
        required = {
            "campaign_id",
            "title",
            "premise",
            "theme",
            "tone",
            "duration_minutes",
            "length_mode",
            "act_count",
            "runtime_location_scoping",
            "recommended_player_count",
            "gameplay_focus",
            "content_warnings",
            "declared_flags",
            "start_beat_id",
            "win_condition",
            "lose_condition",
        }
        missing = sorted(required - set(fragment))
        if missing:
            errors.append("top_level 缺少字段：" + "、".join(missing))
        unexpected = sorted(set(fragment) - required)
        if unexpected:
            errors.append("top_level 包含阶段外字段：" + "、".join(unexpected))
        if fragment.get("campaign_id") != plan.campaign_id_candidate:
            errors.append("top_level.campaign_id 必须等于计划候选 ID")
        if fragment.get("start_beat_id") != plan.start_beat_id:
            errors.append("top_level.start_beat_id 必须等于 StoryPlan")
        if fragment.get("runtime_location_scoping") is not True:
            errors.append("top_level.runtime_location_scoping 必须为 true")
        actual_flags = {str(value) for value in fragment.get("declared_flags", [])}
        if actual_flags != set(registry["flags"]):
            errors.append("top_level.declared_flags 必须精确匹配 StoryPlan flags")
        actual_condition_ids = {
            str(fragment[name].get("id"))
            for name in ("win_condition", "lose_condition")
            if isinstance(fragment.get(name), dict)
        }
        if actual_condition_ids != {"win_condition", "lose_condition"}:
            errors.append(
                "top_level 必须使用固定 win_condition/lose_condition Trigger ID"
            )
        return errors
    else:
        return [f"未知 fragment_kind：{fragment_kind}"]
    values = fragment.get(key)
    if not isinstance(values, list):
        errors.append(f"分片 {fragment_kind} 必须返回 {key} 数组")
        return errors
    actual = {str(item.get("id")) for item in values if isinstance(item, dict)}
    if actual != expected:
        errors.append(
            f"分片 {fragment_kind} 的 {key} 覆盖必须精确匹配 StoryPlan："
            f"缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}"
        )
    if key == "beats":
        plan_beats = {beat.id: beat for beat in plan.beats}
        for raw_beat in values:
            if not isinstance(raw_beat, dict) or raw_beat.get("id") not in plan_beats:
                continue
            planned = plan_beats[str(raw_beat["id"])]
            comparisons = {
                "act_id": planned.act_id,
                "kind": planned.kind,
                "estimated_minutes": planned.estimated_minutes,
                "location_ids": planned.location_ids,
            }
            for field, planned_value in comparisons.items():
                if raw_beat.get(field) != planned_value:
                    errors.append(
                        f"Beat «{planned.id}» 的 {field} 必须与 StoryPlan 完全一致"
                    )
            if not raw_beat.get("objective") or not raw_beat.get("pressure"):
                errors.append(f"Beat «{planned.id}» 缺少 objective 或 pressure")
            actual_clues = {
                str(item.get("id")) for item in raw_beat.get("key_info", [])
            }
            if actual_clues != set(planned.clue_ids):
                errors.append(
                    f"Beat «{planned.id}» 的 KeyInfo 必须精确匹配计划 clue_ids"
                )
            encounter = raw_beat.get("encounter")
            actual_encounter_id = (
                str(encounter.get("id")) if isinstance(encounter, dict) else None
            )
            if actual_encounter_id != planned.encounter_id:
                errors.append(f"Beat «{planned.id}» 的 Encounter 必须与 StoryPlan 一致")
            planned_targets = [exit_.to_beat_id for exit_ in planned.exits]
            actual_targets = [
                str(exit_.get("next_beat_id")) for exit_ in raw_beat.get("exits", [])
            ]
            if actual_targets != planned_targets:
                errors.append(
                    f"Beat «{planned.id}» 的出口顺序与目标必须与 StoryPlan 一致"
                )
            expected_triggers = [
                f"trigger_{planned.id}_{index + 1}"
                for index, _ in enumerate(planned.exits)
            ]
            actual_triggers = [
                str(item.get("id")) for item in raw_beat.get("advance_conditions", [])
            ]
            if actual_triggers != expected_triggers:
                errors.append(
                    f"Beat «{planned.id}» 必须使用代码派生的不可变 Trigger ID"
                )
            actual_exit_triggers = [
                str(item.get("trigger_id")) for item in raw_beat.get("exits", [])
            ]
            if actual_exit_triggers != expected_triggers:
                errors.append(
                    f"Beat «{planned.id}» 的出口必须按顺序绑定派生 Trigger ID"
                )
            actual_actor_ids = {
                str(item.get("actor_id") or item.get("npc_ref"))
                for item in (raw_beat.get("entry_state") or {}).get("actors", [])
            }
            if actual_actor_ids != set(planned.actor_ids):
                errors.append(f"Beat «{planned.id}» 的在场角色必须精确匹配 StoryPlan")
    return errors


def _assemble_canon(
    plan: StoryPlan, fragments: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    raw = dict(fragments["top_level"])
    raw["cast"] = list(fragments["cast"].get("cast", []))
    raw["locations"] = list(fragments["locations"].get("locations", []))
    raw["action_definitions"] = list(fragments["actions"].get("action_definitions", []))
    raw["beats"] = [
        beat
        for act in plan.acts
        for beat in fragments[f"act:{act.id}"].get("beats", [])
    ]
    raw["beats"].extend(fragments["endings"].get("beats", []))
    return raw


async def _repair_assembled_canon(
    raw: dict[str, Any],
    *,
    brief: StoryDesignBrief,
    plan: StoryPlan,
    stage_label: str,
) -> tuple[dict[str, Any], Canon]:
    """对汇总 Canon 执行完整校验，并在失败时做有界修复。

    分片各自通过校验后仍可能因跨分片一致性问题（owner 重复、遭遇/结局计数等）
    在汇总时失败；这里再给一次完整修复机会，避免前面几十次调用前功尽弃。
    """
    for attempt in range(MAX_ASSEMBLY_REPAIRS + 1):
        canon, errors = _canon_errors(raw)
        if canon is not None:
            errors.extend(validate_generated_canon(canon, brief))
            errors.extend(validate_effect_owner_ledger(canon, plan))
        if canon is not None and not errors:
            return raw, canon
        if attempt == MAX_ASSEMBLY_REPAIRS:
            raise StoryGenerationError(f"{stage_label}：" + "；".join(errors))
        raw = await _complete_json(
            build_canon_repair_prompt(raw, errors),
            stage=f"汇总修复（第 {attempt + 1} 次）",
            role=ModelRole.STORY_REPAIR,
        )
    raise AssertionError("汇总修复循环未按预期结束")


def _adjacent_fragment_summaries(
    fragment_kind: str,
    fragments: dict[str, dict[str, Any]],
    plan: StoryPlan,
) -> list[dict[str, Any]]:
    if not fragment_kind.startswith("act:"):
        return []
    act_id = fragment_kind.partition(":")[2]
    index = next((i for i, act in enumerate(plan.acts) if act.id == act_id), -1)
    keys = [
        f"act:{plan.acts[i].id}"
        for i in (index - 1, index + 1)
        if 0 <= i < len(plan.acts)
    ]
    return [
        {
            "fragment_kind": key,
            "beats": [
                {
                    "id": beat.get("id"),
                    "act_id": beat.get("act_id"),
                    "objective": beat.get("objective"),
                    "exits": beat.get("exits", []),
                }
                for beat in fragments.get(key, {}).get("beats", [])
            ],
        }
        for key in keys
        if key in fragments
    ]


def _validate_continuity_review(review: dict[str, Any]) -> None:
    if not isinstance(review.get("passed"), bool) or not isinstance(
        review.get("issues"), list
    ):
        raise StoryGenerationError("连贯性复核输出结构不合法")
    for issue in review["issues"]:
        if not isinstance(issue, dict) or issue.get("severity") not in {
            "error",
            "warning",
        }:
            raise StoryGenerationError("连贯性复核 issue 结构不合法")
    has_errors = any(issue.get("severity") == "error" for issue in review["issues"])
    if review["passed"] == has_errors:
        raise StoryGenerationError("连贯性复核的 passed 与 error issues 不一致")
