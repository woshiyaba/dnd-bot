"""使用真实 LLM 执行故事访谈、Canon 编译和校验修复。"""

from __future__ import annotations

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
    StoryDesignBrief,
    StoryInterviewResponse,
    StoryPlan,
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
    """调用真实 LLM 获取 JSON；提供 Schema 时交给 LangChain 约束输出。"""
    model_name = get_model_name(role)
    try:
        model = get_chat_model(model_name)
        completion_model = (
            # DeepSeek 思考模式不接受 LangChain 强制函数选择；JSON mode
            # 由供应商保证 JSON 语法，再由下方 Pydantic 执行业务结构校验。
            model.with_structured_output(schema, method="json_mode")
            if schema
            else model
        )
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
        if on_stage_start:
            await on_stage_start("plan")
        plan, repairs = await _generate_story_plan(brief, reserved)
        total_repairs += repairs
        if on_artifact:
            await on_artifact("planning", "plan", plan.model_dump(), repairs)
        artifacts["plan"] = plan.model_dump()

    if plan.campaign_id_candidate in reserved and not resumed_plan:
        raise StoryGenerationError("StoryPlan 使用了已占用的 campaign_id")
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
    canon, errors = _canon_errors(raw)
    if canon is not None:
        errors.extend(validate_generated_canon(canon, brief))
        errors.extend(validate_effect_owner_ledger(canon, plan))
    if canon is None or errors:
        raise StoryGenerationError(
            "分片汇总 Canon 未通过完整校验：" + "；".join(errors)
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
        canon, errors = _canon_errors(raw)
        if canon is not None:
            errors.extend(validate_generated_canon(canon, brief))
            errors.extend(validate_effect_owner_ledger(canon, plan))
        if canon is None or errors:
            raise StoryGenerationError(
                "连贯性修复后 Canon 未通过完整校验：" + "；".join(errors)
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
