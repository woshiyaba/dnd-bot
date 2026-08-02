"""故事广场、SQLite 生成任务、限时草稿与 Canon 发布服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
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


class GenerationCancelled(RuntimeError):
    """任务在安全阶段边界响应了取消。"""


class StoryService:
    """单 worker 生成故事，以 SQLite 保存所有可恢复边界。"""

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
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """恢复中断任务，并在服务接收请求前启动顺序消费者。"""
        self._stopping = False
        recovered = self._store.recover_interrupted()
        if recovered:
            logger.warning("[story_worker] 恢复 %d 个中断任务", recovered)
        await self._ensure_worker()

    async def stop(self) -> None:
        """停止消费者；running 记录留给下次启动恢复。"""
        self._stopping = True
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_task = None

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
        )

    def list_stories(self) -> list[StorySummary]:
        """加载磁盘 Canon 并返回不含幕后信息的广场列表。"""
        registry = get_registry()
        registry.load_all(self._canon_dir)
        return [self.summary(canon) for canon in registry.all()]

    async def create_generation_task(
        self, design_brief: dict[str, Any] | StoryDesignBrief
    ) -> StoryGenerationTaskResponse:
        """提交异步生成任务，立即返回可轮询状态。"""
        brief = self._validated_brief(design_brief)
        self._store.purge_expired()
        task_id = secrets.token_urlsafe(24)
        task = self._store.create_task(task_id, brief.model_dump())
        await self._ensure_worker()
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
        if task["status"] == "cancel_requested" and self._worker_task is None:
            self._store.mark_cancelled(task_id)
            task = self._store.get_task(task_id) or task
        return self._task_response(task)

    async def create_draft(
        self, design_brief: dict[str, Any] | StoryDesignBrief
    ) -> StoryDraftResponse:
        """同步兼容包装：仍用真实 LLM，草稿改为 SQLite 持久化。"""
        brief = self._validated_brief(design_brief)
        raw, canon = await generate_canon(confirmed_brief=brief.model_dump())
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
        self, design_brief: dict[str, Any] | StoryDesignBrief
    ) -> StoryDraftResponse:
        """旧同步 HTTP 接口的兼容包装：提交同一任务管线并等待终态。"""
        submitted = await self.create_generation_task(design_brief)
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

    async def _ensure_worker(self) -> None:
        async with self._worker_lock:
            if self._stopping:
                return
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = asyncio.create_task(
                    self._worker_loop(), name="story-generation-worker"
                )

    async def _worker_loop(self) -> None:
        """顺序领取 queued 任务；队列耗尽后退出，由下一次提交再次唤醒。"""
        try:
            while not self._stopping:
                task = self._store.next_queued_task()
                if task is None:
                    return
                await self._run_task(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[story_worker] 消费循环异常退出")

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = task["task_id"]
        repairs = int(task.get("repair_count", 0))

        async def persist(
            stage: str, artifact_key: str, payload: dict[str, Any], attempt: int
        ) -> None:
            nonlocal repairs
            if self._store.is_cancel_requested(task_id):
                raise GenerationCancelled()
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
                raise GenerationCancelled()
            try:
                self._store.begin_stage_attempt(task_id, stage_key)
            except RuntimeError as exc:
                raise StoryGenerationError(str(exc)) from exc

        try:
            if self._store.is_cancel_requested(task_id):
                raise GenerationCancelled()
            artifacts = self._store.artifacts(task_id)
            if "plan" in artifacts:
                campaign_id = str(artifacts["plan"].get("campaign_id_candidate") or "")
                self._validate_campaign_id(campaign_id)
                if not self._store.reserve_campaign_id(task_id, campaign_id):
                    raise StoryGenerationError(
                        "恢复任务的 campaign_id 已被其它任务占用"
                    )
            raw, canon, quality = await generate_staged_canon(
                confirmed_brief=task["design_brief"],
                reserved_campaign_ids=self._store.reserved_campaign_ids(),
                resume_artifacts=artifacts,
                on_artifact=persist,
                on_stage_start=begin_stage,
                initial_repair_count=repairs,
            )
            if self._store.is_cancel_requested(task_id):
                raise GenerationCancelled()
            self._validate_campaign_id(canon.campaign_id)
            draft_id = secrets.token_urlsafe(24)
            self._store.complete_task(
                task_id,
                draft_id=draft_id,
                campaign_id=canon.campaign_id,
                raw=raw,
                quality=quality.model_dump(),
            )
        except GenerationCancelled:
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
            error=task.get("error"),
            draft=draft_response,
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
        elif isinstance(exc, StoryGenerationError):
            raw = str(exc)
            if "campaign_id" in raw or "ID" in raw and "占用" in raw:
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
