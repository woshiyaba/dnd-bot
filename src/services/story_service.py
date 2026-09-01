"""故事广场、SQLite 生成任务、限时草稿与 Canon 发布服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import HTTPException

from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import (
    StoryDesignBrief,
    StoryDraftResponse,
    StoryGenerationTaskResponse,
    StoryInterviewResponse,
    StoryQualityMetrics,
    StorySummary,
)
from src.story.generator import (
    StoryCallContext,
    StoryGenerationCancelled,
    StoryGenerationError,
    continue_interview,
    generate_canon,
    generate_staged_canon,
)
from src.story.loader import DEFAULT_CANON_DIR, get_registry
from src.story.prompt import (
    normalize_confirmed_design_brief,
    validate_confirmed_design_brief,
)
from src.story.store import StoryGenerationStore
from src.story.validation import canon_quality_metrics, validate_generated_canon

logger = logging.getLogger(__name__)

# 允许直接用 ``uvicorn src.app:app`` 启动时也读取故事数据库路径覆盖。
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / ".data" / "story_generation.sqlite3"
_CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?key|authorization|bearer|token|secret)\s*[:=]\s*[^\s,;]+"
)
_URL_PATTERN = re.compile(r"https?://[^\s]+")

_STAGE_PROGRESS = {
    "planning": ("规划故事结构", 10),
    "compiling": ("分片编译 Canon", 25),
    "validating": ("执行完整确定性校验", 80),
    "continuity": ("复核故事连贯性", 90),
    "continuity_repair": ("定向修复受影响 Act", 92),
}
STORY_TASK_CALL_LIMIT = int(os.getenv("STORY_TASK_CALL_LIMIT", "24"))
STORY_TASK_WORKERS = int(os.getenv("STORY_TASK_WORKERS", "2"))
STORY_GLOBAL_LLM_CONCURRENCY = int(os.getenv("STORY_GLOBAL_LLM_CONCURRENCY", "3"))
STORY_FRAGMENT_CONCURRENCY = int(os.getenv("STORY_FRAGMENT_CONCURRENCY", "2"))
STORY_CALL_TIMEOUT_SECONDS = float(os.getenv("STORY_CALL_TIMEOUT_SECONDS", "120"))
STORY_TASK_TIMEOUT_SECONDS = float(os.getenv("STORY_TASK_TIMEOUT_SECONDS", "1200"))
STORY_GLOBAL_TASK_LIMIT = 20
STORY_REQUESTER_ACTIVE_LIMIT = 1
STORY_REQUESTER_DAILY_LIMIT = 5
STORY_INTERVIEW_CALL_LIMIT = 3


class StoryService:
    """用有限 worker 生成故事，以 SQLite 保存所有可恢复边界。"""

    def __init__(
        self,
        canon_dir: Path = DEFAULT_CANON_DIR,
        db_path: Path | None = None,
    ) -> None:
        self._canon_dir = canon_dir
        resolved_db = db_path
        if resolved_db is None:
            configured = os.getenv("STORY_GENERATION_DB_PATH")
            if configured:
                resolved_db = Path(configured)
                if not resolved_db.is_absolute():
                    resolved_db = PROJECT_ROOT / resolved_db
            elif canon_dir == DEFAULT_CANON_DIR:
                resolved_db = DEFAULT_DB_PATH
            else:
                # 测试/嵌入式自定义 canon 目录默认隔离到内存；显式 db_path 仍可测试重启恢复。
                resolved_db = Path(":memory:")
        self._store = StoryGenerationStore(resolved_db)
        self._canon_cache: dict[str, Canon] = {}
        self._publish_lock = asyncio.Lock()
        self._worker_lock = asyncio.Lock()
        self._worker_tasks: set[asyncio.Task[None]] = set()
        self._llm_semaphore: asyncio.Semaphore | None = None
        self._llm_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    async def start(self) -> None:
        """恢复中断任务，并在服务接收请求前启动顺序消费者。"""
        self._stopping = False
        recovered = self._store.recover_interrupted()
        if recovered:
            logger.warning("[story_worker] 恢复 %d 个中断任务", recovered)
        await self._ensure_workers()

    async def stop(self) -> None:
        """停止消费者；running 记录留给下次启动恢复。"""
        self._stopping = True
        tasks = list(self._worker_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_tasks.clear()

    async def interview(
        self,
        *,
        conversation: list[dict[str, Any]],
        design_brief: dict[str, Any],
    ) -> StoryInterviewResponse:
        """调用故事策划 LLM 继续一次无状态访谈。"""
        return await continue_interview(
            conversation=conversation,
            design_brief=design_brief,
            call_context=self._local_call_context(STORY_INTERVIEW_CALL_LIMIT),
        )

    def consume_public_rate_limit(
        self,
        requester_key: str,
        bucket: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> None:
        """消费一个公开故事接口额度。"""
        self._store.consume_rate_limit(
            requester_key,
            bucket,
            limit=limit,
            window=timedelta(seconds=window_seconds),
        )

    def list_stories(self) -> list[StorySummary]:
        """加载磁盘 Canon 并返回不含幕后信息的广场列表。"""
        registry = get_registry()
        registry.load_all(self._canon_dir)
        return [self.summary(canon) for canon in registry.all()]

    async def create_generation_task(
        self,
        design_brief: dict[str, Any] | StoryDesignBrief,
        *,
        requester_key: str | None = None,
    ) -> StoryGenerationTaskResponse:
        """提交异步生成任务，立即返回可轮询状态。"""
        brief = self._validated_brief(design_brief)
        self._store.purge_expired()
        task_id = secrets.token_urlsafe(24)
        task = self._store.create_task(
            task_id,
            brief.model_dump(),
            requester_key=requester_key,
            global_active_limit=STORY_GLOBAL_TASK_LIMIT,
            requester_active_limit=(
                STORY_REQUESTER_ACTIVE_LIMIT if requester_key else None
            ),
            requester_daily_limit=(
                STORY_REQUESTER_DAILY_LIMIT if requester_key else None
            ),
        )
        await self._ensure_workers()
        return self._task_response(task)

    def get_generation_task(self, task_id: str) -> StoryGenerationTaskResponse:
        """读取公开状态；不会返回计划、Canon、NPC 秘密或谜底。"""
        self._store.purge_expired()
        task = self._store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="故事生成任务不存在或已经过期")
        return self._task_response(task)

    def cancel_generation_task(self, task_id: str) -> StoryGenerationTaskResponse:
        """请求取消；正在进行的 LLM 调用完成后在阶段边界生效。"""
        task = self._store.request_cancel(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="故事生成任务不存在或已经过期")
        if task["status"] == "cancel_requested" and not any(
            not worker.done() for worker in self._worker_tasks
        ):
            self._store.mark_cancelled(task_id)
            task = self._store.get_task(task_id) or task
        return self._task_response(task)

    async def create_draft(
        self, design_brief: dict[str, Any] | StoryDesignBrief
    ) -> StoryDraftResponse:
        """同步兼容包装：仍用真实 LLM，草稿改为 SQLite 持久化。"""
        brief = self._validated_brief(design_brief)
        raw, canon = await generate_canon(
            confirmed_brief=brief.model_dump(),
            call_context=self._local_call_context(STORY_TASK_CALL_LIMIT),
        )
        self._validate_campaign_id(canon.campaign_id)
        draft_id = secrets.token_urlsafe(24)
        quality = canon_quality_metrics(canon, continuity_passed=False)
        expires_at = self._store.create_compatibility_draft(
            draft_id=draft_id,
            campaign_id=canon.campaign_id,
            raw=raw,
            quality=quality.model_dump(),
        )
        self._canon_cache[draft_id] = canon
        return StoryDraftResponse(
            draft_id=draft_id,
            expires_at=expires_at,
            story=self.summary(canon),
            quality=quality,
        )

    async def create_draft_via_task(
        self,
        design_brief: dict[str, Any] | StoryDesignBrief,
        *,
        requester_key: str | None = None,
    ) -> StoryDraftResponse:
        """旧同步 HTTP 接口的兼容包装：提交同一任务管线并等待终态。"""
        submitted = await self.create_generation_task(
            design_brief,
            requester_key=requester_key,
        )
        while True:
            task = self._store.get_task(submitted.task_id)
            if task is None:
                raise StoryGenerationError("故事生成任务在完成前过期")
            if task["status"] == "completed":
                response = self._task_response(task)
                if response.draft is None:
                    raise StoryGenerationError("故事任务完成但草稿不存在")
                return response.draft
            if task["status"] == "failed":
                raise StoryGenerationError(task.get("error") or "故事生成失败")
            if task["status"] in {"cancelled", "cancel_requested"}:
                raise StoryGenerationError("故事生成已取消")
            await asyncio.sleep(0.1)

    async def publish(self, draft_id: str) -> StorySummary:
        """从 SQLite 取草稿，重新校验后原子写盘且绝不覆盖 Canon。"""
        async with self._publish_lock:
            draft = self._store.get_draft(draft_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="故事草稿不存在或已经过期")
            try:
                canon = self._canon_cache.get(draft_id) or Canon.from_dict(draft["raw"])
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail="Canon 草稿无法解析"
                ) from exc
            errors = [*validate_canon(canon), *validate_authored_canon(canon)]
            if draft.get("task_id"):
                task = self._store.get_task(draft["task_id"])
                brief = (
                    StoryDesignBrief.model_validate(task["design_brief"])
                    if task is not None
                    else None
                )
                errors.extend(validate_generated_canon(canon, brief))
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail="Canon 发布前校验失败：" + "；".join(errors),
                )
            self._validate_campaign_id(canon.campaign_id)
            self._canon_dir.mkdir(parents=True, exist_ok=True)
            target = self._canon_dir / f"{canon.campaign_id}.json"
            if target.exists() or get_registry().get(canon.campaign_id):
                raise HTTPException(
                    status_code=409, detail="剧本 ID 已存在，请重新生成"
                )

            temporary = (
                self._canon_dir / f".{canon.campaign_id}.{secrets.token_hex(8)}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(draft["raw"], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()

            get_registry().register(canon)
            self._store.mark_published(draft_id)
            self._canon_cache.pop(draft_id, None)
            return self.summary(canon)

    async def _ensure_workers(self) -> None:
        async with self._worker_lock:
            if self._stopping:
                return
            self._worker_tasks = {
                task for task in self._worker_tasks if not task.done()
            }
            while len(self._worker_tasks) < max(1, STORY_TASK_WORKERS):
                worker_number = len(self._worker_tasks) + 1
                task = asyncio.create_task(
                    self._worker_loop(worker_number),
                    name=f"story-generation-worker-{worker_number}",
                )
                self._worker_tasks.add(task)

    async def _worker_loop(self, worker_number: int) -> None:
        """顺序领取 queued 任务；多个 worker 通过 SQLite 原子避免重复领取。"""
        try:
            while not self._stopping:
                task = self._store.next_queued_task()
                if task is None:
                    return
                await self._run_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[story_worker] 消费循环异常退出 | worker=%d", worker_number
            )

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        repairs = int(task.get("repair_count", 0))

        async def persist(
            stage: str, artifact_key: str, payload: dict[str, Any], attempt: int
        ) -> None:
            nonlocal repairs
            if self._store.is_cancel_requested(task_id):
                raise StoryGenerationCancelled()
            repairs += max(0, attempt)
            if artifact_key == "plan":
                campaign_id = str(payload.get("campaign_id_candidate") or "")
                self._validate_campaign_id(campaign_id)
                if (
                    self._canon_dir / f"{campaign_id}.json"
                ).exists() or not self._store.reserve_campaign_id(task_id, campaign_id):
                    raise StoryGenerationError("计划生成的 campaign_id 已被占用")
            self._store.save_artifact(
                task_id,
                stage=stage,
                artifact_key=artifact_key,
                payload=payload,
                attempt=attempt,
            )
            label, base_progress = _STAGE_PROGRESS.get(stage, (stage, 20))
            if artifact_key.startswith("fragment:"):
                count = len(
                    [
                        key
                        for key in self._store.artifacts(task_id)
                        if key.startswith("fragment:")
                    ]
                )
                base_progress = min(78, 20 + count * 5)
            self._store.update_task(
                task_id,
                stage=label,
                progress=base_progress,
                repair_count=repairs,
            )

        async def begin_stage(stage_key: str) -> None:
            if self._store.is_cancel_requested(task_id):
                raise StoryGenerationCancelled()
            try:
                self._store.begin_stage_attempt(task_id, stage_key)
            except RuntimeError as exc:
                raise StoryGenerationError(str(exc)) from exc

        async def reserve_call(stage: str) -> int:
            if self._store.is_cancel_requested(task_id):
                raise StoryGenerationCancelled()
            count = self._store.reserve_llm_call(
                task_id,
                limit=STORY_TASK_CALL_LIMIT,
            )
            if count is None:
                if self._store.is_cancel_requested(task_id):
                    raise StoryGenerationCancelled()
                raise StoryGenerationError(
                    f"模型调用预算已用尽（{STORY_TASK_CALL_LIMIT}/{STORY_TASK_CALL_LIMIT}）"
                )
            logger.info(
                "[story_worker] 模型调用 | task_id=%s | stage=%s | call=%d/%d",
                task_id,
                stage,
                count,
                STORY_TASK_CALL_LIMIT,
            )
            return count

        try:
            if self._store.is_cancel_requested(task_id):
                raise StoryGenerationCancelled()
            artifacts = self._store.artifacts(task_id)
            if "plan" in artifacts:
                campaign_id = str(artifacts["plan"].get("campaign_id_candidate") or "")
                self._validate_campaign_id(campaign_id)
                if not self._store.reserve_campaign_id(task_id, campaign_id):
                    raise StoryGenerationError(
                        "恢复任务的 campaign_id 已被其它任务占用"
                    )
            async with asyncio.timeout(STORY_TASK_TIMEOUT_SECONDS):
                raw, canon, quality = await generate_staged_canon(
                    confirmed_brief=task["design_brief"],
                    reserved_campaign_ids=self._store.reserved_campaign_ids(),
                    resume_artifacts=artifacts,
                    on_artifact=persist,
                    on_stage_start=begin_stage,
                    initial_repair_count=repairs,
                    call_context=StoryCallContext(
                        reserve_call=reserve_call,
                        semaphore=self._story_llm_semaphore(),
                        timeout_seconds=STORY_CALL_TIMEOUT_SECONDS,
                    ),
                    fragment_concurrency=STORY_FRAGMENT_CONCURRENCY,
                )
            if self._store.is_cancel_requested(task_id):
                raise StoryGenerationCancelled()
            self._validate_campaign_id(canon.campaign_id)
            draft_id = secrets.token_urlsafe(24)
            self._store.complete_task(
                task_id,
                draft_id=draft_id,
                campaign_id=canon.campaign_id,
                raw=raw,
                quality=quality.model_dump(),
            )
        except StoryGenerationCancelled:
            self._store.mark_cancelled(task_id)
        except Exception as exc:
            logger.exception("[story_worker] 任务失败 | task_id=%s", task_id)
            self._store.mark_failed(task_id, self._public_error(exc))

    def _task_response(self, task: dict[str, Any]) -> StoryGenerationTaskResponse:
        draft_response = None
        if task.get("draft_id"):
            draft = self._store.get_draft(task["draft_id"])
            if draft is not None:
                canon = Canon.from_dict(draft["raw"])
                quality = (
                    StoryQualityMetrics.model_validate(draft["quality"])
                    if draft.get("quality")
                    else None
                )
                draft_response = StoryDraftResponse(
                    draft_id=draft["draft_id"],
                    expires_at=draft["expires_at"],
                    story=self.summary(canon),
                    quality=quality,
                )
        return StoryGenerationTaskResponse(
            task_id=task["task_id"],
            status=task["status"],
            stage=task["stage"],
            progress=task["progress"],
            created_at=task["created_at"],
            updated_at=task["updated_at"],
            llm_calls_used=task.get("llm_call_count", 0),
            llm_calls_limit=STORY_TASK_CALL_LIMIT,
            error=task.get("error"),
            draft=draft_response,
        )

    def _story_llm_semaphore(self) -> asyncio.Semaphore:
        """按当前事件循环懒建全局故事模型信号量。"""
        loop = asyncio.get_running_loop()
        if self._llm_semaphore is None or self._llm_loop is not loop:
            self._llm_loop = loop
            self._llm_semaphore = asyncio.Semaphore(
                max(1, STORY_GLOBAL_LLM_CONCURRENCY)
            )
        return self._llm_semaphore

    def _local_call_context(self, limit: int) -> StoryCallContext:
        """为访谈和旧同步入口创建进程内调用预算。"""
        used = 0
        lock = asyncio.Lock()

        async def reserve(stage: str) -> int:
            nonlocal used
            async with lock:
                if used >= limit:
                    raise StoryGenerationError(f"模型调用预算已用尽（{limit}/{limit}）")
                used += 1
                logger.info(
                    "[story_service] 模型调用 | stage=%s | call=%d/%d",
                    stage,
                    used,
                    limit,
                )
                return used

        return StoryCallContext(
            reserve_call=reserve,
            semaphore=self._story_llm_semaphore(),
            timeout_seconds=STORY_CALL_TIMEOUT_SECONDS,
        )

    @staticmethod
    def summary(canon: Canon) -> StorySummary:
        """从 Canon 生成严格脱敏的故事广场摘要。"""
        return StorySummary(
            campaign_id=canon.campaign_id,
            title=canon.title,
            premise=canon.premise,
            theme=canon.theme,
            tone=canon.tone,
            duration_minutes=canon.duration_minutes,
            recommended_player_count=canon.recommended_player_count,
            gameplay_focus=list(canon.gameplay_focus),
            content_warnings=list(canon.content_warnings),
            beat_count=len(canon.beats),
        )

    @staticmethod
    def _validated_brief(
        design_brief: dict[str, Any] | StoryDesignBrief,
    ) -> StoryDesignBrief:
        errors = validate_confirmed_design_brief(design_brief)
        if errors:
            raise HTTPException(status_code=422, detail="；".join(errors))
        return normalize_confirmed_design_brief(design_brief)

    @staticmethod
    def _validate_campaign_id(campaign_id: str) -> None:
        if not _CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
            raise HTTPException(
                status_code=422,
                detail="campaign_id 必须是 3–64 位 lowercase snake_case",
            )

    @staticmethod
    def _public_error(exc: Exception) -> str:
        """只公开阶段性失败原因，移除 URL、令牌与供应商细节。"""
        if isinstance(exc, HTTPException):
            text = str(exc.detail)
        elif isinstance(exc, TimeoutError):
            text = "故事生成超过 20 分钟，任务已停止"
        elif isinstance(exc, StoryGenerationError):
            raw = str(exc)
            if "模型调用预算已用尽" in raw:
                text = f"故事生成已达到 {STORY_TASK_CALL_LIMIT} 次模型调用上限"
            elif "campaign_id" in raw or "ID" in raw and "占用" in raw:
                text = "故事 ID 已被占用，请重新提交生成"
            elif "LLM 调用失败" in raw:
                text = "故事生成模型调用失败，请稍后重试"
            elif "JSON" in raw or "解析" in raw:
                text = "故事生成模型返回内容无法解析，请重试"
            elif "重启重试上限" in raw:
                text = "故事生成阶段在服务重启后仍未完成，请重新提交任务"
            else:
                text = "故事结构在限定修复次数内未通过校验"
        else:
            text = "故事生成服务发生内部错误"
        text = _SECRET_PATTERN.sub(r"\1=[已隐藏]", text)
        text = _URL_PATTERN.sub("[地址已隐藏]", text)
        return text[:1000]


story_service = StoryService()
