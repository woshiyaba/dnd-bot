"""使用真实 LLM 执行故事访谈、Canon 编译和校验修复。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import ModelRole, get_chat_model, get_model_name
from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import (
    CanonDraft,
    StoryDesignBrief,
    StoryContinuityReview,
    StoryInterviewResponse,
    StoryPlan,
    StoryPlanCandidate,
    StoryQualityMetrics,
    continuity_repair_schema,
    story_plan_section_repair_schema,
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
MAX_INTERVIEW_REPAIRS = 1
MAX_CANON_REPAIRS = 2
MAX_FRAGMENT_REPAIRS = 2
MAX_STORY_PLAN_LOCAL_REPAIRS = 2
MAX_STORY_PLAN_REPLANS = 1
DEFAULT_CALL_TIMEOUT_SECONDS = 120.0

ArtifactCallback = Callable[[str, str, dict[str, Any], int], Awaitable[None]]
StageStartCallback = Callable[[str], Awaitable[None]]
CallReservationCallback = Callable[[str], Awaitable[int]]


class StoryGenerationError(RuntimeError):
    """真实 LLM 未能返回可用的故事结构或 Canon。"""


class StoryGenerationCancelled(RuntimeError):
    """故事任务在下一次模型调用前响应取消。"""


@dataclass(slots=True)
class StoryCallContext:
    """一次故事请求共享的调用预算、并发和超时边界。"""

    reserve_call: CallReservationCallback
    semaphore: asyncio.Semaphore
    timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS


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
    call_context: StoryCallContext | None = None,
) -> dict[str, Any]:
    """调用真实 LLM 获取 JSON，并对传输和格式各恢复一次。"""
    model_name = get_model_name(role)
    try:
        model = get_chat_model(model_name)
        completion_model = (
            # DeepSeek 思考模式不接受 LangChain 强制函数选择；JSON mode
            # include_raw 让 Schema 失败时仍能把候选交给业务修复循环。
            model.with_structured_output(
                schema,
                method="json_mode",
                include_raw=True,
            )
            if schema
            else model
        )
    except Exception as exc:
        raise StoryGenerationError(f"故事 {stage} 的模型初始化失败：{exc}") from exc

    current_prompt = prompt
    for parse_attempt in range(2):
        response = await _invoke_story_model(
            completion_model,
            current_prompt,
            stage=stage,
            model_name=model_name,
            call_context=call_context,
        )
        parsed, raw_text = _structured_response(response, schema)
        if parsed is not None:
            return parsed
        if parse_attempt == 0:
            current_prompt = (
                prompt + "\n\n上一次输出无法解析为 JSON 对象。请重新生成完整结果，"
                "只输出一个 JSON 对象，不要解释或使用 Markdown。"
                + (f"\n上次输出：{raw_text[:4000]}" if raw_text else "")
            )
            logger.warning(
                "[story_generator] JSON 恢复 | stage=%s | model=%s | raw_chars=%d",
                stage,
                model_name,
                len(raw_text),
            )
    raise StoryGenerationError(f"故事 {stage} 的 LLM 输出不是可解析的 JSON 对象")


async def _invoke_story_model(
    completion_model: Any,
    prompt: str,
    *,
    stage: str,
    model_name: str,
    call_context: StoryCallContext | None,
) -> Any:
    """执行一次可计数请求；仅对瞬时传输错误额外尝试一次。"""
    retryable = (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
        TimeoutError,
    )
    for transport_attempt in range(2):
        try:
            if call_context is None:
                async with asyncio.timeout(DEFAULT_CALL_TIMEOUT_SECONDS):
                    return await completion_model.ainvoke(prompt)
            async with call_context.semaphore:
                await call_context.reserve_call(stage)
                async with asyncio.timeout(call_context.timeout_seconds):
                    return await completion_model.ainvoke(prompt)
        except (StoryGenerationCancelled, StoryGenerationError):
            raise
        except retryable as exc:
            if transport_attempt == 0:
                logger.warning(
                    "[story_generator] 瞬时调用失败，准备重试 | stage=%s | model=%s | error=%s",
                    stage,
                    model_name,
                    type(exc).__name__,
                )
                continue
            raise StoryGenerationError(
                f"故事 {stage} 的 LLM 调用失败：{type(exc).__name__}"
            ) from exc
        except Exception as exc:
            logger.exception(
                "[story_generator] LLM 调用失败 | stage=%s | model=%s",
                stage,
                model_name,
            )
            raise StoryGenerationError(f"故事 {stage} 的 LLM 调用失败：{exc}") from exc
    raise AssertionError("故事传输重试循环未按预期结束")


def _structured_response(
    response: Any, schema: type[BaseModel] | None
) -> tuple[dict[str, Any] | None, str]:
    """优先取结构化结果，失败时保留原始 JSON 候选供业务层修复。"""
    if schema is not None and isinstance(response, dict) and "raw" in response:
        structured = response.get("parsed")
        if structured is not None:
            try:
                value = (
                    structured
                    if isinstance(structured, schema)
                    else schema.model_validate(structured)
                )
                return value.model_dump(exclude_none=True, by_alias=True), ""
            except (TypeError, ValidationError):
                pass
        raw_text = _message_text(response.get("raw"))
        return extract_json_object(raw_text), raw_text
    if schema is not None:
        try:
            value = (
                response
                if isinstance(response, schema)
                else schema.model_validate(response)
            )
            return value.model_dump(exclude_none=True, by_alias=True), ""
        except (TypeError, ValidationError):
            pass
    raw_text = _message_text(response)
    return extract_json_object(raw_text), raw_text


def _log_repair_attempt(
    *,
    stage: str,
    repair_round: int,
    errors: list[str],
    prompt: str,
    max_attempts: int,
) -> None:
    """只记录修复元数据，不把故事正文和隐藏 Canon 写入日志。"""
    logger.info(
        "[story_generator] 开始%s | 修复轮次=%d/%d | 待修复问题=%d 个 "
        "| prompt_chars=%d",
        stage,
        repair_round,
        max_attempts,
        len(errors),
        len(prompt),
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
    *,
    conversation: list[dict[str, Any]],
    design_brief: dict[str, Any],
    call_context: StoryCallContext | None = None,
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
        call_context=call_context,
    )
    for attempt in range(MAX_INTERVIEW_REPAIRS + 1):
        try:
            return StoryInterviewResponse.model_validate(raw)
        except ValidationError as exc:
            errors = _story_interview_validation_errors(exc)
            if attempt == MAX_INTERVIEW_REPAIRS:
                raise StoryGenerationError(
                    f"故事访谈输出在 {MAX_INTERVIEW_REPAIRS} 次修复后仍不合法："
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
            max_attempts=MAX_INTERVIEW_REPAIRS,
        )
        raw = await _complete_json(
            repair_prompt,
            stage=f"访谈修复（第 {repair_round} 次）",
            role=ModelRole.STORY_REPAIR,
            schema=StoryInterviewResponse,
            call_context=call_context,
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
    *,
    confirmed_brief: dict[str, Any],
    call_context: StoryCallContext | None = None,
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
        schema=CanonDraft,
        call_context=call_context,
    )

    for attempt in range(MAX_CANON_REPAIRS + 1):
        canon, errors = _canon_errors(draft)
        if canon is not None and not errors:
            return draft, canon
        if attempt == MAX_CANON_REPAIRS:
            raise StoryGenerationError(
                "Canon 在两次修复后仍未通过校验：" + "；".join(errors)
            )
        draft = await _complete_json(
            build_canon_repair_prompt(draft, errors),
            stage=f"修复（第 {attempt + 1} 次）",
            role=ModelRole.STORY_REPAIR,
            schema=CanonDraft,
            call_context=call_context,
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
    call_context: StoryCallContext | None = None,
    fragment_concurrency: int = 2,
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
        plan, repairs = await _generate_story_plan(
            brief,
            reserved,
            call_context=call_context,
        )
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
    fragment_gate = asyncio.Semaphore(max(1, int(fragment_concurrency)))

    async def compile_wave(fragment_kinds: list[str]) -> int:
        """并发生成一波无相互依赖的分片，并保留已经验证的结果。"""
        pending: list[str] = []
        for fragment_kind in fragment_kinds:
            artifact_key = f"fragment:{fragment_kind}"
            if artifact_key not in artifacts:
                pending.append(fragment_kind)
                continue
            fragment = artifacts[artifact_key]
            errors = _fragment_errors(
                fragment_kind,
                fragment,
                plan,
                registry,
                fragments,
            )
            if errors:
                raise StoryGenerationError(
                    f"已持久化分片 {fragment_kind} 校验失败：" + "；".join(errors)
                )
            fragments[fragment_kind] = fragment

        generated: dict[str, tuple[dict[str, Any], int]] = {}

        async def compile_one(fragment_kind: str) -> None:
            artifact_key = f"fragment:{fragment_kind}"
            async with fragment_gate:
                if on_stage_start:
                    await on_stage_start(artifact_key)
                fragment, repairs = await _generate_fragment(
                    fragment_kind=fragment_kind,
                    brief=brief,
                    plan=plan,
                    registry=registry,
                    ledger=ledger,
                    reference_fragments=references,
                    adjacent_fragments=_adjacent_plan_summaries(fragment_kind, plan),
                    compiled_fragments=fragments,
                    call_context=call_context,
                )
                if on_artifact:
                    await on_artifact("compiling", artifact_key, fragment, repairs)
                artifacts[artifact_key] = fragment
                generated[fragment_kind] = (fragment, repairs)

        try:
            async with asyncio.TaskGroup() as group:
                for fragment_kind in pending:
                    group.create_task(compile_one(fragment_kind))
        except* StoryGenerationCancelled as group:
            raise group.exceptions[0]
        except* StoryGenerationError as group:
            raise group.exceptions[0]

        for fragment_kind in fragment_kinds:
            if fragment_kind in generated:
                fragments[fragment_kind] = generated[fragment_kind][0]
        return sum(repairs for _, repairs in generated.values())

    total_repairs += await compile_wave(["top_level", "cast", "locations", "actions"])
    total_repairs += await compile_wave(
        [*(f"act:{act.id}" for act in plan.acts), "endings"]
    )

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
            schema=StoryContinuityReview,
            call_context=call_context,
        )
        if on_artifact:
            await on_artifact("continuity", "continuity_review", review, 0)
    _validate_continuity_review(review)
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
                schema=continuity_repair_schema(affected_ids),
                call_context=call_context,
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
                schema=StoryContinuityReview,
                call_context=call_context,
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
    brief: StoryDesignBrief,
    reserved_campaign_ids: list[str],
    *,
    call_context: StoryCallContext | None = None,
) -> tuple[StoryPlan, int]:
    """生成并归一化 StoryPlan，按问题类别执行有界修复。"""
    raw = await _complete_json(
        build_story_plan_prompt(brief, reserved_campaign_ids=reserved_campaign_ids)
        + "\n生成前额外自检：branch_points.choices 必须是源 Beat 的不同出口 Beat ID，"
        "不是选择文案；剧情分支须在高潮前汇流，最终胜负结局不作为 meaningful branch；"
        "effect_owner_ledger 的 owner_kind 与 owner_id 必须遵守 schema 中的 ID 类别配对。",
        stage="计划",
        role=ModelRole.STORY_PLANNING,
        schema=StoryPlanCandidate,
        call_context=call_context,
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
                schema=StoryPlanCandidate,
                call_context=call_context,
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
        repair_schema = story_plan_section_repair_schema(sections)
        repair = await _complete_json(
            repair_prompt,
            stage=f"计划局部修复（第 {local_repairs + 1} 次）",
            role=ModelRole.STORY_REPAIR,
            schema=repair_schema,
            call_context=call_context,
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
    call_context: StoryCallContext | None = None,
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
        call_context=call_context,
    )
    previous_fingerprint: tuple[str, ...] | None = None
    for attempt in range(MAX_FRAGMENT_REPAIRS + 1):
        errors = _fragment_errors(
            fragment_kind, raw, plan, registry, compiled_fragments
        )
        if not errors:
            return raw, attempt
        fingerprint = tuple(sorted(errors))
        if fingerprint == previous_fingerprint:
            raise StoryGenerationError(
                f"分片 {fragment_kind} 的校验问题连续两轮未变化：" + "；".join(errors)
            )
        previous_fingerprint = fingerprint
        if attempt == MAX_FRAGMENT_REPAIRS:
            raise StoryGenerationError(
                f"分片 {fragment_kind} 在两次修复后仍不合法：" + "；".join(errors)
            )
        repair_prompt = build_fragment_repair_prompt(
            fragment_kind=fragment_kind,
            fragment=raw,
            validation_errors=errors,
            confirmed_brief=brief,
            story_plan=plan.model_dump(),
            id_registry=registry,
            effect_owner_ledger=ledger,
        )
        _log_repair_attempt(
            stage=f"分片 {fragment_kind} 修复",
            repair_round=attempt + 1,
            errors=errors,
            prompt=repair_prompt,
            max_attempts=MAX_FRAGMENT_REPAIRS,
        )
        raw = await _complete_json(
            repair_prompt,
            stage=f"分片 {fragment_kind} 修复（第 {attempt + 1} 次）",
            role=ModelRole.STORY_REPAIR,
            call_context=call_context,
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


def _adjacent_plan_summaries(
    fragment_kind: str,
    plan: StoryPlan,
) -> list[dict[str, Any]]:
    """只从 StoryPlan 提取相邻 Act 摘要，避免创作分片形成串行依赖。"""
    if not fragment_kind.startswith("act:"):
        return []
    act_id = fragment_kind.partition(":")[2]
    index = next((i for i, act in enumerate(plan.acts) if act.id == act_id), -1)
    return [
        {
            "fragment_kind": f"act:{plan.acts[i].id}",
            "purpose": plan.acts[i].purpose,
            "turning_point": plan.acts[i].turning_point,
            "beats": [
                {
                    "id": beat.id,
                    "act_id": beat.act_id,
                    "objective": beat.objective,
                    "pressure": beat.pressure,
                    "exits": [item.model_dump() for item in beat.exits],
                }
                for beat in plan.beats
                if beat.act_id == plan.acts[i].id
            ],
        }
        for i in (index - 1, index + 1)
        if 0 <= i < len(plan.acts)
    ]


def _validate_continuity_review(review: dict[str, Any]) -> None:
    try:
        StoryContinuityReview.model_validate(review)
    except ValidationError as exc:
        raise StoryGenerationError(f"连贯性复核输出结构不合法：{exc}") from exc
