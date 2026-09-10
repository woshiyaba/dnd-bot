"""使用真实 LLM 执行故事访谈、Canon 编译和校验修复。"""

from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import re
from copy import deepcopy
from time import perf_counter
from pathlib import Path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
    LengthFinishReasonError,
)
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import ModelRole, get_chat_model, get_model_name
from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import (
    CanonDraft,
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
    PlanEntities,
    PlanPayoffDetail,
    PlanRouteText,
    PlanSimpleBatch,
    StoryDesignBrief,
    StoryContinuityReview,
    StoryInterviewResponse,
    StoryPlan,
    StoryPlanCandidate,
    StoryPlanFrame,
    StoryPlanWorkState,
    StoryPlanCore,
    StoryQualityMetrics,
    story_plan_section_repair_schema,
    canon_fragment_schema,
    canon_object_repair_schema,
    story_entity_roster_schema,
    story_execution_plan_schema,
)
from src.story.prompt import (
    build_canon_authoring_prompt,
    build_canon_repair_prompt,
    build_continuity_review_prompt,
    build_fragment_prompt,
    build_fragment_repair_prompt,
    build_story_plan_prompt,
    build_compact_plan_constraints,
    build_frozen_entity_constraints,
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
    validate_canon_playability,
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
MAX_ASSEMBLY_REPAIRS = 2
GENERATION_VERSION = 4

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
    ensure_budget: Callable[[int], None] | None = None


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
    if "json" not in prompt.lower():
        prompt = "只输出一个完整 JSON 对象。\n" + prompt
    model_name = get_model_name(role)
    options: dict[str, Any] = {
        "max_tokens": (
            12288
            if schema is not None
            and schema.__name__ in {"StoryPlanCandidate", "StoryExecutionPlan"}
            else 8192
        )
    }
    if model_name.partition("/")[2].startswith("deepseek-v4"):
        # ChatOpenAI 会重命名 max_tokens，DeepSeek 的原生字段通过 extra_body 发送。
        options["extra_body"] = {"max_tokens": options.pop("max_tokens")}
        # V4 默认 high 思考会让 JSON 编译超时；创作事实和复核仍保留 low 思考。
        # https://api-docs.deepseek.com/guides/thinking_mode/
        if (
            role in {ModelRole.STORY_AUTHORING, ModelRole.STORY_REPAIR}
            or schema is not None
            and schema.__name__ in {"StoryPlanCandidate", "StoryExecutionPlan"}
        ):
            options["extra_body"]["thinking"] = {"type": "disabled"}
        elif role in {
            ModelRole.STORY_PLANNING,
            ModelRole.STORY_PLANNING_FAST,
            ModelRole.STORY_REPAIR,
            ModelRole.STORY_CONTINUITY,
        }:
            options["reasoning_effort"] = "low"
    try:
        model = get_chat_model(model_name)
        if schema is not None:
            # DeepSeek 思考模式不接受 LangChain 强制函数选择；JSON mode
            # include_raw 让 Schema 失败时仍能把候选交给业务修复循环。
            completion_model = model.with_structured_output(
                schema,
                method="json_mode",
                include_raw=True,
                # 必须绑定在底层模型；include_raw 的 RunnableMap 不转发 ainvoke kwargs。
                **options,
            )
        else:
            # 无 Schema 的小阶段也要求供应商直接返回 JSON 对象。
            completion_model = model.bind(
                response_format={"type": "json_object"}, **options
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
        raw_message = (
            response.get("raw")
            if isinstance(response, dict) and "raw" in response
            else response
        )
        metadata = getattr(raw_message, "response_metadata", {})
        truncated = (
            isinstance(metadata, dict) and metadata.get("finish_reason") == "length"
        )
        if parsed is not None and not truncated:
            return parsed
        if parse_attempt == 0:
            if truncated and model_name.partition("/")[2].startswith("deepseek-v4"):
                options["extra_body"]["thinking"] = {"type": "disabled"}
                options.pop("reasoning_effort", None)
                completion_model = (
                    model.with_structured_output(
                        schema, method="json_mode", include_raw=True, **options
                    )
                    if schema is not None
                    else model.bind(response_format={"type": "json_object"}, **options)
                )
            current_prompt = (
                prompt + "\n\n上一次输出无法解析为 JSON 对象。请重新生成完整结果，"
                "只输出一个 JSON 对象，不要解释或使用 Markdown。若输出被截断，请缩短各字段文字，保留全部目标对象。"
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
    """计数、限流、超时和观测统一覆盖每次真实请求。"""
    retryable = (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
        TimeoutError,
    )
    for attempt in range(2):
        queued_at = perf_counter()
        started_at = None
        status = "cancelled"
        response = None
        try:
            semaphore = call_context.semaphore if call_context else asyncio.Semaphore(1)
            async with semaphore:
                if call_context:
                    await call_context.reserve_call(stage)
                started_at = perf_counter()
                async with asyncio.timeout(
                    call_context.timeout_seconds
                    if call_context
                    else DEFAULT_CALL_TIMEOUT_SECONDS
                ):
                    response = await completion_model.ainvoke(prompt)
                status = "completed"
                return response
        except LengthFinishReasonError as exc:
            # SDK 在 JSON mode 也可能先抛截断异常；保留真实正文，交给上层一次格式修复。
            usage = exc.completion.usage
            response = AIMessage(
                content=exc.completion.choices[0].message.content or "",
                response_metadata={"finish_reason": "length"},
                usage_metadata=(
                    {
                        "input_tokens": usage.prompt_tokens,
                        "output_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    }
                    if usage
                    else None
                ),
            )
            status = "truncated"
            return response
        except (StoryGenerationCancelled, StoryGenerationError):
            raise
        except retryable as exc:
            status = type(exc).__name__
            if attempt == 1:
                raise StoryGenerationError(
                    f"故事 {stage} 的 LLM 调用失败：{status}"
                ) from exc
            retry_after = getattr(getattr(exc, "response", None), "headers", {}).get(
                "retry-after"
            )
            try:
                delay = max(1.0, float(retry_after)) if retry_after else 1.0
            except (TypeError, ValueError):
                delay = 1.0
            logger.warning(
                "[story_generator] 瞬时调用失败 | stage=%s | error=%s | retry_after=%.1f",
                stage,
                status,
                delay,
            )
            await asyncio.sleep(delay)
        except Exception as exc:
            status = type(exc).__name__
            logger.exception(
                "[story_generator] LLM 调用失败 | stage=%s | model=%s",
                stage,
                model_name,
            )
            raise StoryGenerationError(
                f"故事 {stage} 的 LLM 调用失败：{status}"
            ) from exc
        finally:
            finished_at = perf_counter()
            raw = (
                response.get("raw")
                if isinstance(response, dict) and "raw" in response
                else response
            )
            usage = getattr(raw, "usage_metadata", None)
            metadata = getattr(raw, "response_metadata", None)
            logger.info(
                "[story_call] stage=%s model=%s attempt=%d status=%s wait_ms=%d elapsed_ms=%d prompt_chars=%d output_chars=%d tokens=%s finish_reason=%s",
                stage,
                model_name,
                attempt + 1,
                status,
                int(((started_at or finished_at) - queued_at) * 1000),
                int((finished_at - (started_at or finished_at)) * 1000),
                len(prompt),
                len(_message_text(raw)) if raw is not None else 0,
                (
                    {
                        key: usage.get(key)
                        for key in ("input_tokens", "output_tokens", "total_tokens")
                    }
                    if isinstance(usage, dict)
                    else None
                ),
                metadata.get("finish_reason") if isinstance(metadata, dict) else None,
            )
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
        "| prompt_chars=%d | schema_fields=%s",
        stage,
        repair_round,
        max_attempts,
        len(errors),
        len(prompt),
        [
            error.partition(":")[0]
            for error in errors
            if re.match(r"^[a-z_][a-z_0-9.]*:", error)
        ][:8],
    )


def _story_plan_errors(plan: StoryPlan, brief: StoryDesignBrief) -> list[str]:
    """执行计划校验，并兼容分支 schema 与旧确认稿的最小并行预算语义。"""
    return [issue.message for issue in _story_plan_issues(plan, brief)]


def _story_plan_issues(
    plan: StoryPlan, brief: StoryDesignBrief
) -> list[PlanValidationIssue]:
    """执行结构化计划校验并兼容旧确认稿的并行预算语义。"""
    issues = validate_story_plan_issues(plan, brief)
    if plan.plan_version >= 2:
        issues.extend(
            PlanValidationIssue(
                code="fixed_topology",
                path=("beats",),
                category="structural",
                affected_sections=frozenset({"beats", "branch_points"}),
                message=error,
            )
            for error in _fixed_topology_errors(plan, brief)
        )
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
    """紧凑规划、并发创作、对象修复与绑定正文版本的可恢复验收。"""
    brief = normalize_confirmed_design_brief(confirmed_brief)
    artifacts = dict(resume_artifacts or {})
    references = _load_reference_fragments()
    context = {
        "version": GENERATION_VERSION,
        "brief_hash": _fingerprint(brief.model_dump()),
        "references_hash": _fingerprint(references),
    }
    if "generation_context" in artifacts and artifacts["generation_context"] != context:
        raise StoryGenerationError("生成版本、设计稿或参考资料已改变，请重新提交任务")
    if "plan" not in artifacts and any(
        key.startswith("plan:") and key not in {"plan:core", "plan:entities"}
        for key in artifacts
    ):
        raise StoryGenerationError(
            "旧版渐进规划中间产物不能用于新版生成，请重新提交任务"
        )
    total_repairs = max(0, initial_repair_count)

    async def persist(
        stage: str, key: str, payload: dict[str, Any], attempt: int = 0
    ) -> None:
        nonlocal total_repairs
        if on_artifact:
            await on_artifact(stage, key, payload, attempt)
        artifacts[key] = payload
        total_repairs += attempt

    async def begin(key: str) -> None:
        if on_stage_start:
            await on_stage_start(key)

    if "generation_context" not in artifacts:
        await persist("planning", "generation_context", context)
    if "plan" in artifacts:
        plan = StoryPlan.model_validate(artifacts["plan"])
        errors = _story_plan_errors(plan, brief)
        if errors:
            raise StoryGenerationError(
                "已持久化 StoryPlan 校验失败：" + "；".join(errors)
            )
    else:
        if call_context and call_context.ensure_budget:
            call_context.ensure_budget(brief.scale_profile.acts + 11)
        reserved = sorted(
            set(reserved_campaign_ids or [])
            | {path.stem for path in CANON_DIR.glob("*.json")}
        )
        plan, repairs = await _generate_compact_story_plan(
            brief,
            reserved,
            artifacts=artifacts,
            on_artifact=persist,
            on_stage_start=on_stage_start,
            call_context=call_context,
        )
        await persist("planning", "plan", plan.model_dump(), repairs)

    registry = story_plan_id_registry(plan)
    ledger = [item.model_dump() for item in plan.effect_owner_ledger]
    fragments: dict[str, dict[str, Any]] = {
        "story_core": artifacts.get("plan:core", {})
    }
    gate = asyncio.Semaphore(max(1, int(fragment_concurrency)))
    kinds = [
        "top_level",
        "cast",
        "locations",
        "actions",
        *(f"act:{act.id}" for act in plan.acts),
        "endings",
    ]
    if call_context and call_context.ensure_budget:
        missing = sum(
            f"fragment:{kind}" not in artifacts
            for kind in kinds
            if kind != "actions" or plan.entities.actions
        )
        snapshot_hash = _fingerprint(artifacts.get("assembled_canon", {}).get("canon"))
        reviewed = any(
            artifacts.get(key, {}).get("canon_hash") == snapshot_hash
            for key in ("continuity_review", "continuity_review_final")
        )
        call_context.ensure_budget(missing + int(bool(missing) or not reviewed))

    async def compile_wave(wave: list[str]) -> None:
        pending = []
        for kind in wave:
            key = f"fragment:{kind}"
            if kind == "actions" and not plan.entities.actions and key not in artifacts:
                await persist("compiling", key, {"action_definitions": []})
            if key not in artifacts:
                pending.append(kind)
                continue
            fragment = artifacts[key]
            errors = _fragment_errors(kind, fragment, plan, registry, fragments)
            if errors:
                raise StoryGenerationError(
                    f"已持久化分片 {kind} 校验失败：" + "；".join(errors)
                )
            fragments[kind] = fragment
        generated = {}

        async def compile_one(kind: str) -> None:
            async with gate:
                await begin(f"fragment:{kind}")
                fragment, repairs = await _generate_fragment(
                    fragment_kind=kind,
                    brief=brief,
                    plan=plan,
                    registry=registry,
                    ledger=ledger,
                    reference_fragments=references,
                    adjacent_fragments=_adjacent_plan_summaries(kind, plan),
                    compiled_fragments=fragments,
                    call_context=call_context,
                )
                await persist("compiling", f"fragment:{kind}", fragment, repairs)
                generated[kind] = fragment

        try:
            async with asyncio.TaskGroup() as group:
                for kind in pending:
                    group.create_task(compile_one(kind))
        except* StoryGenerationCancelled as group:
            raise group.exceptions[0]
        except* StoryGenerationError as group:
            raise group.exceptions[0]
        fragments.update(generated)

    await compile_wave(kinds[:4])
    await compile_wave(kinds[4:])
    base_raw = _assemble_canon(plan, fragments)
    input_hash = _fingerprint({"plan": plan.model_dump(), "canon": base_raw})
    snapshot = artifacts.get("assembled_canon", {})
    if (
        snapshot.get("input_hash") == input_hash
        and snapshot.get("version") == GENERATION_VERSION
    ):
        raw = snapshot["canon"]
        total_repairs = max(total_repairs, int(snapshot.get("repair_count", 0)))
        canon, errors = _full_canon_errors(raw, brief, plan)
        if errors:
            raise StoryGenerationError(
                "已持久化 Canon 快照校验失败：" + "；".join(errors)
            )
    else:
        await begin("assembled_canon")
        raw, canon, repairs = await _repair_assembled_canon(
            base_raw,
            brief=brief,
            plan=plan,
            stage_label="分片汇总 Canon 未通过完整校验",
            call_context=call_context,
        )
        snapshot = {
            "version": GENERATION_VERSION,
            "input_hash": input_hash,
            "canon": raw,
            "repair_count": total_repairs + repairs,
            "continuity_repaired": False,
        }
        await persist("validating", "assembled_canon", snapshot, repairs)

    while True:
        repaired = bool(snapshot.get("continuity_repaired"))
        key = "continuity_review_final" if repaired else "continuity_review"
        canon_hash = _fingerprint(raw)
        cached = artifacts.get(key, {})
        if cached.get("canon_hash") == canon_hash:
            review = cached["report"]
            _validate_continuity_review(review)
        else:
            await begin(key)
            review = await _complete_json(
                build_continuity_review_prompt(
                    confirmed_brief=brief,
                    canon=raw,
                    story_core=artifacts.get("plan:core"),
                    previous_review=(
                        artifacts.get("continuity_review", {}).get("report")
                        if repaired
                        else None
                    ),
                    changed_object_ids=snapshot.get("continuity_repair_ids"),
                ),
                stage="修复后连贯性复核" if repaired else "连贯性复核",
                role=ModelRole.STORY_CONTINUITY,
                schema=StoryContinuityReview,
                call_context=call_context,
            )
            _validate_continuity_review(review)
            await persist(
                "continuity", key, {"canon_hash": canon_hash, "report": review}
            )
        issues = [issue for issue in review["issues"] if issue["severity"] == "error"]
        if not issues:
            break
        if repaired:
            raise StoryGenerationError("定向修复后仍有连贯性错误，任务终止")
        ids = set()
        for issue in issues:
            object_ids = issue.get("affected_object_ids", [])
            if object_ids:
                ids.update(str(value) for value in object_ids)
            else:
                ids.update(
                    beat.id
                    for beat in plan.beats
                    if beat.act_id in issue.get("affected_act_ids", [])
                )
        if not ids:
            raise StoryGenerationError("连贯性复核错误缺少合法受影响对象")
        await begin("continuity_repair")
        raw = await _repair_canon_objects(
            raw,
            ids=ids,
            errors=[issue["message"] for issue in issues],
            brief=brief,
            plan=plan,
            call_context=call_context,
        )
        canon, errors = _full_canon_errors(raw, brief, plan)
        if errors:
            raise StoryGenerationError("连贯性修复未通过完整校验：" + "；".join(errors))
        snapshot = {
            "version": GENERATION_VERSION,
            "input_hash": input_hash,
            "canon": raw,
            "repair_count": total_repairs + 1,
            "continuity_repaired": True,
            "continuity_repair_ids": sorted(ids),
        }
        # 整个候选先校验，再以单个快照原子保存，避免部分 Act 已更新而标记未提交。
        await persist("continuity_repair", "assembled_canon", snapshot, 1)

    metrics = canon_quality_metrics(
        canon, repair_count=total_repairs, continuity_passed=True
    )
    metrics.quality_notes.append(
        "已检查声明的机械路径；检定结果、自由行动与叙事条件经过模型复核，仍需实际试玩验证"
    )
    if any(issue["severity"] == "warning" for issue in review["issues"]):
        metrics.quality_notes.append("剧本通过验收，另有非阻断的创作建议")
    return raw, canon, metrics


def _fingerprint(value: Any) -> str:
    """为确定性 JSON 产物生成依赖指纹，不包含日志正文。"""
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


async def _generate_compact_story_plan(
    brief: StoryDesignBrief,
    reserved: list[str],
    *,
    artifacts: dict[str, dict[str, Any]],
    on_artifact: ArtifactCallback,
    on_stage_start: StageStartCallback | None,
    call_context: StoryCallContext | None,
) -> tuple[StoryPlan, int]:
    """先生成全局创作事实，再一次编译紧凑的完整计划。"""
    core, _ = await _run_plan_stage(
        artifact_key="plan:core",
        label="故事核心与章节",
        schema=StoryPlanCore,
        brief=brief,
        state=StoryPlanWorkState(),
        target={
            "target_id": "core",
            "act_count": brief.scale_profile.acts,
            "playable_beat_count": brief.scale_profile.playable_beats,
        },
        instructions="一次确定全局真相、角色动机、因果链、结局意图和 Act 骨架。每 Act 1–3 个可玩 Beat，数量严格匹配确认稿。玩家身份由 brief.player_role 给定，不要把玩家另写成 NPC；未命名的玩家主角统一称玩家，不替玩家起名，已出现的玩家人物姓名列入 player_character_names。每场战斗必须有区别于玩家的明确对手。引擎的失败推进针对调查、交流等非团灭挫折；任意遭遇整队战败均进入失败结局，失败结局不能只适用于最终决战。不要生成正文或战斗卡面。",
        artifacts=artifacts,
        validate=lambda value: _validate_plan_frame(
            value, brief, reserved, check_reserved=True
        ),
        resumed_validate=lambda value: _validate_plan_frame(
            value, brief, reserved, check_reserved=False
        ),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
        reserved_campaign_ids=reserved,
        call_context=call_context,
    )
    roster, _ = await _run_plan_stage(
        artifact_key="plan:entities",
        label="角色与地点名册",
        schema=story_entity_roster_schema(
            brief.scale_profile.locations,
            brief.scale_profile.encounters,
            brief.scale_profile.clues,
        ),
        brief=brief,
        state=StoryPlanWorkState(),
        target={"story_core": core.model_dump()},
        instructions=(
            "只生成实体名册，不生成 Beat 或卡面。严格按 schema 的数组长度创建地点、遭遇和线索，"
            "每个对象用唯一且明确的类别前缀 ID；已有 Act ID 和 ending_win/ending_lose 不得重复使用。"
            "actors 只登记非玩家角色与怪物，kind 必须为 npc 或 monster，不复制 brief.player_role 中的玩家。"
            "每场战斗都要在 actors 中登记真实对手，encounters 是战斗本身，不是敌方角色。"
            "flags/items/actions 只创建必要的少量项；没有规则道具或特殊行动时留空。所有叙事说明用简体中文。"
        ),
        artifacts=artifacts,
        validate=lambda value: _entity_roster_errors(
            value, {act.id for act in core.acts}, set(core.player_character_names)
        ),
        on_artifact=on_artifact,
        on_stage_start=on_stage_start,
        call_context=call_context,
        role=ModelRole.STORY_AUTHORING,
    )
    if on_stage_start:
        await on_stage_start("plan")
    plan, repairs = await _generate_story_plan(
        brief,
        [value for value in reserved if value != core.campaign_id_candidate],
        story_core=core.model_dump(),
        frozen_entities=roster.model_dump(exclude_none=True),
        call_context=call_context,
    )
    if [act.id for act in plan.acts] != [act.id for act in core.acts]:
        raise StoryGenerationError("执行计划不得改变已经确认的 Act 骨架")
    return plan, repairs


def _entity_roster_errors(
    roster: PlanEntities, act_ids: set[str], player_names: set[str]
) -> list[str]:
    """名册先验收数量、非玩家类型与跨类别 ID；后续计划只能引用这些事实。"""
    ids = [
        item.id
        for category in type(roster).model_fields
        for item in getattr(roster, category)
    ]
    errors = [
        f"实体 ID «{value}» 重复或占用了章节/结局 ID"
        for value in set(ids)
        if ids.count(value) > 1 or value in act_ids | {"ending_win", "ending_lose"}
    ]
    errors.extend(
        f"角色 «{actor.id}» 必须是明确的非玩家 npc 或 monster；玩家由运行时 party 提供"
        for actor in roster.actors
        if actor.kind not in {"npc", "monster"}
    )
    errors.extend(
        f"玩家人物 «{actor.name}» 已由 party 扮演，不得重复登记为 actor «{actor.id}»"
        for actor in roster.actors
        if actor.name in player_names
    )
    return errors


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
    call_context: StoryCallContext | None = None,
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
        call_context=call_context,
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
            call_context=call_context,
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
        call_context=call_context,
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
            call_context=call_context,
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
        call_context=call_context,
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
                call_context=call_context,
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
            call_context=call_context,
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
            call_context=call_context,
        )
        return beat_id, list(batch.items), used

    # 各 Beat 的出口文案彼此独立，并发生成以显著缩短端到端延迟。
    async with asyncio.TaskGroup() as group:
        route_tasks = [
            group.create_task(build_route(beat_id)) for beat_id in placement_ids
        ]
    route_results = [task.result() for task in route_tasks]
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
            call_context=call_context,
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
            call_context=call_context,
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
        call_context=call_context,
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
            call_context=call_context,
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
        call_context=call_context,
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
    call_context: StoryCallContext | None = None,
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
            schema=schema,
            call_context=call_context,
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
            schema=schema,
            call_context=call_context,
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
    *,
    call_context: StoryCallContext | None = None,
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
        call_context=call_context,
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
    brief: StoryDesignBrief,
    reserved_campaign_ids: list[str],
    *,
    call_context: StoryCallContext | None = None,
    story_core: dict[str, Any] | None = None,
    frozen_entities: dict[str, Any] | None = None,
) -> tuple[StoryPlan, int]:
    """生成并归一化 StoryPlan，按问题类别执行有界修复。"""
    plan_schema = (
        story_execution_plan_schema(frozen_entities)
        if frozen_entities is not None
        else StoryPlanCandidate
    )
    raw = await _complete_json(
        build_story_plan_prompt(
            brief,
            reserved_campaign_ids=reserved_campaign_ids,
            story_core=story_core,
            frozen_entities=frozen_entities,
        )
        + "\n生成前额外自检：branch_points.choices 必须是源 Beat 的不同出口 Beat ID，"
        "不是选择文案；剧情分支须在高潮前汇流，最终胜负结局不作为 meaningful branch；"
        "effect_owner_ledger 的 owner_kind 与 owner_id 必须遵守 schema 中的 ID 类别配对。",
        stage="计划",
        role=ModelRole.STORY_PLANNING,
        schema=plan_schema,
        call_context=call_context,
    )
    if frozen_entities is not None and raw.get("endings") is None:
        raw["endings"] = {}
    previous_fingerprint: tuple[tuple[str, tuple[str | int, ...]], ...] | None = None
    local_repairs = 0
    replans = 0

    while True:
        if frozen_entities is not None:
            raw["entities"] = deepcopy(frozen_entities)
            if not frozen_entities["flags"]:
                raw["foreshadowing_payoffs"] = []
            if not frozen_entities["flags"] and not frozen_entities["items"]:
                raw["effect_owner_ledger"] = []
        if story_core is not None:
            raw["campaign_id_candidate"] = story_core["campaign_id_candidate"]
            raw["plan_version"] = GENERATION_VERSION
            if isinstance(raw.get("beats"), list):
                playable = [
                    beat
                    for beat in raw["beats"]
                    if isinstance(beat, dict) and beat.get("kind") != "ending"
                ]
                slots = [
                    act["id"]
                    for act in story_core["acts"]
                    for _ in range(act["playable_beat_count"])
                ]
                if len(playable) == len(slots):
                    # 章节归属来自已验收骨架的顺序与配额，不让模型重复分配。
                    raw["acts"] = [
                        {
                            key: value
                            for key, value in act.items()
                            if key != "playable_beat_count"
                        }
                        for act in story_core["acts"]
                    ]
                    for beat, act_id in zip(playable, slots):
                        beat["act_id"] = act_id
                    for beat in raw["beats"]:
                        if isinstance(beat, dict) and beat.get("kind") == "ending":
                            beat["act_id"] = story_core["acts"][-1]["id"]
        plan, issues, raw = _validate_story_plan_candidate(raw, brief)
        if plan is not None and story_core is not None:
            expected = {
                act["id"]: act["playable_beat_count"] for act in story_core["acts"]
            }
            actual = {
                act.id: sum(
                    beat.act_id == act.id and beat.kind != "ending"
                    for beat in plan.beats
                )
                for act in plan.acts
            }
            if actual != expected:
                issues.append(
                    PlanValidationIssue(
                        code="core_acts",
                        path=("acts",),
                        category="structural",
                        affected_sections=frozenset({"acts", "beats"}),
                        message=f"Act ID 和可玩 Beat 数必须保持故事核心骨架：{expected}",
                    )
                )
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
                frozen_entities=frozen_entities,
            )
            if story_core is not None:
                repair_prompt += build_compact_plan_constraints(brief, story_core)
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
                schema=plan_schema,
                call_context=call_context,
            )
            if frozen_entities is not None and raw.get("endings") is None:
                raw["endings"] = {}
            replans += 1
            continue

        if local_repairs >= MAX_STORY_PLAN_LOCAL_REPAIRS:
            raise StoryGenerationError(
                "StoryPlan 局部修复预算耗尽：" + "；".join(errors)
            )
        sections = affected_story_plan_sections(issues)
        if frozen_entities is not None:
            sections.discard("entities")
        repair_prompt = build_story_plan_repair_prompt(
            candidate=raw,
            confirmed_brief=brief,
            issues=issues,
            affected_sections=sections,
        )
        if story_core is not None:
            repair_prompt += build_compact_plan_constraints(brief, story_core)
        if frozen_entities is not None:
            repair_prompt += build_frozen_entity_constraints(frozen_entities)
        repair_prompt += "\n本轮必须实际修改下列校验失败字段，不能原样复制旧区段。允许修改分钟数和线索归属，只有 ID 与出口拓扑固定：\n" + "\n".join(
            errors
        )
        _log_repair_attempt(
            stage="StoryPlan 局部修复",
            repair_round=local_repairs + 1,
            errors=errors,
            prompt=repair_prompt,
            max_attempts=MAX_STORY_PLAN_LOCAL_REPAIRS,
        )
        repair_schema = story_plan_section_repair_schema(sections)
        for format_attempt in range(2):
            repair = await _complete_json(
                repair_prompt,
                stage=f"计划局部修复（第 {local_repairs + 1} 次）",
                role=ModelRole.STORY_REPAIR,
                schema=repair_schema,
                call_context=call_context,
            )
            try:
                repair = repair_schema.model_validate(repair).model_dump(
                    exclude_none=True
                )
                break
            except ValidationError as exc:
                detail = "；".join(_story_interview_validation_errors(exc))
                if format_attempt:
                    raise StoryGenerationError(
                        "计划局部修复输出结构不合法：" + detail
                    ) from exc
                repair_prompt += (
                    "\n上次修复输出的层级或字段类型不符合 schema，请重新输出。sections.beats 必须直接是数组，不得再包一层 beats 对象。错误："
                    + detail
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
    brief: StoryDesignBrief | None = None,
) -> dict[str, Any]:
    """把可由 StoryPlan 确定性推导的机械字段回填进分片，再交给确定性校验。

    模型只需写叙事内容（objective/pressure、出口文案、线索正文、遭遇细节等），
    而 id、act_id、kind、estimated_minutes、location_ids、exits 与 Trigger ID 这些
    「必须逐字符与计划一致」的字段由代码强制生成，从源头消除最脆弱的一类校验失败。
    """
    fragment = deepcopy(fragment)
    if fragment_kind == "top_level":
        fragment = dict(fragment)
        fragment["campaign_id"] = plan.campaign_id_candidate
        fragment["start_beat_id"] = plan.start_beat_id
        fragment["runtime_location_scoping"] = True
        fragment["declared_flags"] = sorted(item.id for item in plan.entities.flags)
        if brief is not None:
            fragment.update(
                duration_minutes=brief.duration_minutes,
                length_mode=brief.length_mode,
                act_count=len(plan.acts),
                recommended_player_count=brief.player_count,
                tone=brief.tone,
                gameplay_focus=list(brief.gameplay_focus),
                content_warnings=list(brief.content_warnings),
            )
        for name in ("win_condition", "lose_condition"):
            condition = getattr(plan, name)
            if condition is not None:
                fragment[name] = condition.model_dump(exclude_none=True)
        return fragment

    if not (fragment_kind.startswith("act:") or fragment_kind == "endings"):
        return fragment

    plan_beats = {beat.id: beat for beat in plan.beats}
    plan_actors = {actor.id: actor for actor in plan.entities.actors}
    normalized_beats: list[dict[str, Any]] = []
    if not isinstance(fragment.get("beats"), list):
        return fragment
    for raw_beat in fragment["beats"]:
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
        beat["payoff_flag_ids"] = list(planned.payoff_flag_ids)
        if planned.kind == "ending":
            outcome = next(
                route.outcome
                for route in plan.ending_routes
                if route.ending_id == planned.id
            )
            beat["ending_outcome"] = outcome
            if outcome == "lose" and isinstance(beat.get("entry_state"), dict):
                beat["entry_state"].update(
                    location_id=None, preserve_current_scene=True
                )
                if plan.plan_version >= 4:
                    # 败局继承战败现场，不把计划中的角色再次实例化到场景中。
                    beat["entry_state"]["actors"] = []
        # 出口与推进条件 Trigger ID 完全由计划推导，不信任模型逐字符复写。
        beat["exits"] = [
            {
                "trigger_id": f"trigger_{beat_id}_{index + 1}",
                "next_beat_id": exit_.to_beat_id,
            }
            for index, exit_ in enumerate(planned.exits)
        ]
        conditions = beat.get("advance_conditions")
        if plan.plan_version >= 2:
            conditions = [
                exit_.trigger.model_dump(exclude_none=True)
                for exit_ in planned.exits
                if exit_.trigger is not None
            ]
            beat["advance_conditions"] = conditions
        if isinstance(conditions, list):
            for index, trigger in enumerate(conditions):
                if isinstance(trigger, dict):
                    trigger["id"] = f"trigger_{beat_id}_{index + 1}"
        encounter = beat.get("encounter")
        if isinstance(encounter, dict) and planned.encounter_id:
            encounter["id"] = planned.encounter_id
            if plan.plan_version >= 3:
                encounter["monster_ids"] = list(planned.enemy_actor_ids)
        entry = beat.get("entry_state")
        if (
            plan.plan_version >= 3
            and isinstance(entry, dict)
            and isinstance(entry.get("actors"), list)
        ):
            for actor in entry["actors"]:
                if not isinstance(actor, dict):
                    continue
                actor_id = actor.get("actor_id")
                spec = plan_actors.get(actor_id) if isinstance(actor_id, str) else None
                if spec is not None and spec.kind in {"npc", "monster"}:
                    actor["name"] = spec.name
                    actor["type"] = spec.kind
                    # 卡面只引用已验证 Cast；运行时 build_beat_scene 已负责装配。
                    actor.pop("card", None)
        # 线索正文保留模型创作，只把 id 与顺序对齐到计划的 clue_ids。
        clues = beat.get("key_info")
        if isinstance(clues, list):
            if plan.plan_version >= 3:
                for clue in clues:
                    if not isinstance(clue, dict):
                        continue
                    effects = clue.get("discovery_effects")
                    flags = (
                        effects.get("flags_set") if isinstance(effects, dict) else None
                    )
                    clue_id = clue.get("id")
                    if (
                        isinstance(flags, dict)
                        and isinstance(clue_id, str)
                        and flags.get(clue_id) is True
                    ):
                        # 发现自身由引擎 discovered_clues 记录，不重复编译成同名 Flag。
                        flags.pop(clue_id)
            planned_clue_ids = list(planned.clue_ids)
            by_id = {
                str(item.get("id")): item for item in clues if isinstance(item, dict)
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
            compiled_fragments=compiled_fragments,
        ),
        stage=f"分片 {fragment_kind}",
        role=ModelRole.STORY_AUTHORING,
        schema=canon_fragment_schema(
            "act" if fragment_kind.startswith("act:") else fragment_kind,
            authoring=plan.plan_version >= 3,
        ),
        call_context=call_context,
    )
    previous_fingerprint: tuple[str, ...] | None = None
    for attempt in range(MAX_FRAGMENT_REPAIRS + 1):
        raw = _enforce_fragment_constants(fragment_kind, raw, plan, brief)
        errors = _fragment_errors(
            fragment_kind, raw, plan, registry, compiled_fragments
        )
        if not errors:
            schema = canon_fragment_schema(
                "act" if fragment_kind.startswith("act:") else fragment_kind
            )
            return schema.model_validate(raw).model_dump(exclude_none=True), attempt
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
        targets = set(re.findall(r"«([^»]+)»", "；".join(errors)))
        for error in errors:
            match = re.match(
                r"(cast|locations|action_definitions|beats)\.(\d+)\.", error
            )
            if match:
                collection, index = match.group(1), int(match.group(2))
                rows = raw.get(collection, [])
                if (
                    index < len(rows)
                    and isinstance(rows[index], dict)
                    and rows[index].get("id")
                ):
                    targets.add(rows[index]["id"])
        objects = _canon_objects(raw)
        if targets and any(
            key in targets or key.partition(":")[2] in targets for key in objects
        ):
            raw = await _repair_canon_objects(
                raw,
                ids=targets,
                errors=errors,
                brief=brief,
                plan=plan,
                call_context=call_context,
            )
            continue
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
            schema=canon_fragment_schema(
                "act" if fragment_kind.startswith("act:") else fragment_kind,
                authoring=plan.plan_version >= 3,
            ),
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
    try:
        schema = canon_fragment_schema(
            "act" if fragment_kind.startswith("act:") else fragment_kind
        )
        fragment = schema.model_validate(fragment).model_dump(exclude_none=True)
    except ValidationError as exc:
        return _story_interview_validation_errors(exc)
    except KeyError:
        return [f"未知 fragment_kind：{fragment_kind}"]
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
        for name in ("win_condition", "lose_condition"):
            condition = getattr(plan, name)
            if condition is not None and fragment[name] != condition.model_dump(
                exclude_none=True
            ):
                errors.append(f"top_level.{name} 必须与 StoryPlan 一致")
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
                "payoff_flag_ids": planned.payoff_flag_ids,
            }
            for field, planned_value in comparisons.items():
                if raw_beat.get(field) != planned_value:
                    errors.append(
                        f"Beat «{planned.id}» 的 {field} 必须与 StoryPlan 完全一致"
                    )
            if not raw_beat.get("objective") or not raw_beat.get("pressure"):
                errors.append(f"Beat «{planned.id}» 缺少 objective 或 pressure")
            if planned.kind == "ending":
                outcome = next(
                    route.outcome
                    for route in plan.ending_routes
                    if route.ending_id == planned.id
                )
                if raw_beat.get("ending_outcome") != outcome:
                    errors.append(
                        f"Beat «{planned.id}» 的胜负类型必须与 StoryPlan 一致"
                    )
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
            if plan.plan_version >= 3:
                if (
                    planned.encounter_id
                    and (raw_beat.get("encounter") or {}).get("monster_ids")
                    != planned.enemy_actor_ids
                ):
                    errors.append(
                        f"Beat «{planned.id}» 的敌方名单必须与 StoryPlan.enemy_actor_ids 一致"
                    )
                actors = {actor.id: actor for actor in plan.entities.actors}
                for actor in raw_beat["entry_state"]["actors"]:
                    spec = actors.get(actor["actor_id"])
                    if spec is not None and (
                        actor["type"] != spec.kind or actor["name"] != spec.name
                    ):
                        errors.append(
                            f"Beat «{planned.id}» 的角色 «{spec.id}» 身份与类型必须与 StoryPlan 一致"
                        )
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
            if plan.plan_version >= 2:
                expected_conditions = [
                    exit_.trigger.model_dump(exclude_none=True)
                    for exit_ in planned.exits
                    if exit_.trigger is not None
                ]
                if raw_beat["advance_conditions"] != expected_conditions:
                    errors.append(
                        f"Beat «{planned.id}» 的触发条件必须与 StoryPlan 一致"
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
            inherited_scene = plan.plan_version >= 4 and planned.id == "ending_lose"
            if inherited_scene and (
                raw_beat["entry_state"].get("preserve_current_scene") is not True
                or raw_beat["entry_state"].get("location_id") is not None
            ):
                errors.append("失败结局必须继承当前战败现场，不得重设地点")
            if actual_actor_ids != set(planned.actor_ids) and not (
                inherited_scene and not actual_actor_ids
            ):
                errors.append(
                    f"Beat «{planned.id}» 的在场角色必须精确匹配 StoryPlan；缺少 {sorted(set(planned.actor_ids) - actual_actor_ids)}，多余 {sorted(actual_actor_ids - set(planned.actor_ids))}；补齐缺少角色的 actor_id/location_id/disposition，保留已有正确角色"
                )
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


def _canon_fragments(raw: dict[str, Any], plan: StoryPlan) -> dict[str, dict[str, Any]]:
    """按同一计划还原分片，供初次生成、修复、恢复统一验收。"""
    collections = {"cast", "locations", "action_definitions", "beats"}
    return {
        "top_level": {
            key: value for key, value in raw.items() if key not in collections
        },
        "cast": {"cast": raw.get("cast", [])},
        "locations": {"locations": raw.get("locations", [])},
        "actions": {"action_definitions": raw.get("action_definitions", [])},
        **{
            f"act:{act.id}": {
                "beats": [
                    beat
                    for beat in raw.get("beats", [])
                    if beat.get("act_id") == act.id and beat.get("kind") != "ending"
                ]
            }
            for act in plan.acts
        },
        "endings": {
            "beats": [
                beat for beat in raw.get("beats", []) if beat.get("kind") == "ending"
            ]
        },
    }


def _full_canon_errors(
    raw: dict[str, Any], brief: StoryDesignBrief, plan: StoryPlan
) -> tuple[Canon | None, list[str]]:
    """所有发布候选都经过相同的结构、计划、规则与玩法检查。"""
    try:
        normalized = CanonDraft.model_validate(raw).model_dump(exclude_none=True)
    except ValidationError as exc:
        return None, _story_interview_validation_errors(exc)
    canon, errors = _canon_errors(normalized)
    if canon is None:
        return canon, errors
    fragments = _canon_fragments(normalized, plan)
    registry = story_plan_id_registry(plan)
    for kind, fragment in fragments.items():
        errors.extend(_fragment_errors(kind, fragment, plan, registry, fragments))
    errors.extend(validate_generated_canon(canon, brief))
    errors.extend(validate_effect_owner_ledger(canon, plan))
    errors.extend(validate_canon_playability(canon))
    if plan.plan_version >= 2:
        expected = {
            "duration_minutes": brief.duration_minutes,
            "length_mode": brief.length_mode,
            "act_count": len(plan.acts),
            "recommended_player_count": brief.player_count,
            "tone": brief.tone,
            "gameplay_focus": list(brief.gameplay_focus),
            "content_warnings": list(brief.content_warnings),
        }
        errors.extend(
            f"top_level.{field} 必须与确认稿一致"
            for field, value in expected.items()
            if normalized.get(field) != value
        )
    return canon, list(dict.fromkeys(errors))


def _canon_objects(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    collections = ("cast", "locations", "action_definitions", "beats")
    return {
        "top_level": {
            key: value for key, value in raw.items() if key not in collections
        },
        **{
            f"{kind}:{item['id']}": item
            for kind in collections
            for item in (raw.get(kind) or [])
            if isinstance(item, dict) and item.get("id")
        },
    }


async def _repair_canon_objects(
    raw: dict[str, Any],
    *,
    ids: set[str],
    errors: list[str],
    brief: StoryDesignBrief,
    plan: StoryPlan,
    call_context: StoryCallContext | None,
) -> dict[str, Any]:
    """只替换明确定位的对象；任何白名单之外的修改均不接纳。"""
    objects = _canon_objects(raw)
    ids = set(ids)
    ids.update(
        owner.owner_id for owner in plan.effect_owner_ledger if owner.effect_id in ids
    )
    for beat in raw.get("beats", []):
        if not isinstance(beat, dict):
            continue
        children = [
            *(beat.get("key_info") or []),
            *(beat.get("advance_conditions") or []),
            beat.get("encounter") or {},
        ]
        if any(
            isinstance(child, dict) and child.get("id") in ids for child in children
        ):
            ids.add(beat["id"])
    targets = sorted(
        key for key in objects if key in ids or key.partition(":")[2] in ids
    )
    if not targets:
        raise StoryGenerationError("校验问题无法定位到可修复对象：" + "；".join(errors))
    schema = canon_object_repair_schema(targets, authoring=plan.plan_version >= 3)
    output_schema = canon_object_repair_schema(targets)
    location_context = {}
    if any(key.startswith("locations:") for key in targets):
        location_context = {
            "locations": raw.get("locations", []),
            "beat_routes": [
                {
                    "id": beat["id"],
                    "location_ids": beat.get("location_ids", []),
                    "entry_location_id": (beat.get("entry_state") or {}).get(
                        "location_id"
                    ),
                    "required_location_ids": [
                        clue.get("location_id") for clue in beat.get("key_info", [])
                    ]
                    + (
                        [beat["encounter"].get("location_id")]
                        if beat.get("encounter")
                        else []
                    ),
                }
                for beat in raw.get("beats", [])
                if isinstance(beat, dict)
            ],
        }
    prompt = (
        "修复以下对象的明确错误，返回 JSON，根对象必须只有 objects 字段，"
        "objects 内的键必须精确匹配待修复对象。"
        "每项返回完整对象，保留 ID、Beat 拓扑、所属关系、时间、已锁定触发条件与效果 owner。"
        "不得改写未列出的对象，不得加入模板或离线故事。\n"
        "新计划中 entry_state.actors 只写 actor_id/location_id/disposition；name/type/card 从计划和 Cast 装配，不重复输出。\n"
        f"返回层级示意：{json.dumps({'objects': dict.fromkeys(targets, {})}, ensure_ascii=False)}\n"
        f"<schema>{json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(',', ':'))}</schema>\n"
        f"<errors>{json.dumps(errors, ensure_ascii=False)}</errors>\n"
        f"<brief>{json.dumps(brief.model_dump(), ensure_ascii=False, separators=(',', ':'))}</brief>\n"
        f"<plan>{json.dumps(plan.model_dump(exclude_none=True), ensure_ascii=False, separators=(',', ':'))}</plan>\n"
        f"<readonly_location_context>{json.dumps(location_context, ensure_ascii=False, separators=(',', ':'))}</readonly_location_context>\n"
        f"<objects>{json.dumps({key: objects[key] for key in targets}, ensure_ascii=False, separators=(',', ':'))}</objects>"
        "\n只按 errors 修复指定对象。若修复 ending_lose，必须适用于任意遭遇战败：不得写固定战场、固定观众、某个特定对手或最终决战；保留当前现场，任务失败的共同后果可作为战败后的叙述。不得改写全局胜负条件。"
    )
    for attempt in range(2):
        _log_repair_attempt(
            stage="对象定向修复",
            repair_round=attempt + 1,
            errors=errors,
            prompt=prompt,
            max_attempts=2,
        )
        replacement = await _complete_json(
            prompt,
            stage="对象定向修复",
            role=ModelRole.STORY_REPAIR,
            schema=schema,
            call_context=call_context,
        )
        if plan.plan_version >= 3 and isinstance(replacement.get("objects"), dict):
            for key, value in replacement["objects"].items():
                if key == "top_level" and key in targets and isinstance(value, dict):
                    replacement["objects"][key] = _enforce_fragment_constants(
                        "top_level", value, plan, brief
                    )
                if (
                    key in targets
                    and key.startswith("beats:")
                    and isinstance(value, dict)
                ):
                    if value.get("id") != key.partition(":")[2]:
                        raise StoryGenerationError(
                            f"对象修复不得更改 ID «{key.partition(':')[2]}»"
                        )
                    replacement["objects"][key] = _enforce_fragment_constants(
                        "endings", {"beats": [value]}, plan, brief
                    )["beats"][0]
        try:
            replacement = output_schema.model_validate(replacement).model_dump(
                by_alias=True, exclude_none=True
            )["objects"]
            break
        except ValidationError as exc:
            detail = "；".join(_story_interview_validation_errors(exc))
            if attempt:
                raise StoryGenerationError("对象定向修复结构不合法：" + detail) from exc
            prompt += (
                "\n上次返回的结构不合法，请按 schema 重新输出完整修复对象：" + detail
            )
    result = deepcopy(raw)
    for key in targets:
        kind, _, object_id = key.partition(":")
        value = replacement[key]
        if kind == "top_level":
            result.update(value)
        else:
            if value["id"] != object_id:
                raise StoryGenerationError(f"对象修复不得更改 ID «{object_id}»")
            result[kind] = [
                (
                    value
                    if isinstance(item, dict) and item.get("id") == object_id
                    else item
                )
                for item in result[kind]
            ]
    return result


async def _repair_assembled_canon(
    raw: dict[str, Any],
    *,
    brief: StoryDesignBrief,
    plan: StoryPlan,
    stage_label: str,
    call_context: StoryCallContext | None = None,
) -> tuple[dict[str, Any], Canon, int]:
    """汇总阶段只修复错误对象，所有轮次重新执行完整验收。"""
    previous_errors = None
    for attempt in range(MAX_ASSEMBLY_REPAIRS + 1):
        canon, errors = _full_canon_errors(raw, brief, plan)
        if canon is not None and not errors:
            return raw, canon, attempt
        fingerprint = tuple(sorted(errors))
        if attempt == MAX_ASSEMBLY_REPAIRS or fingerprint == previous_errors:
            raise StoryGenerationError(f"{stage_label}：" + "；".join(errors))
        previous_errors = fingerprint
        ids = {value for error in errors for value in re.findall(r"«([^»]+)»", error)}
        if any(
            "top_level" in error
            or "Canon." in error
            or "win_condition" in error
            or "lose_condition" in error
            for error in errors
        ):
            ids.add("top_level")
        raw = await _repair_canon_objects(
            raw,
            ids=ids,
            errors=errors,
            brief=brief,
            plan=plan,
            call_context=call_context,
        )
    raise AssertionError("汇总修复循环未按预期结束")


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
