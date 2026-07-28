"""故事广场、限时预览草稿与 Canon 发布服务。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import StoryDraftResponse, StoryInterviewResponse, StorySummary
from src.story.generator import continue_interview, generate_canon
from src.story.loader import DEFAULT_CANON_DIR, get_registry
from src.story.prompt import validate_confirmed_design_brief

_CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DRAFT_TTL = timedelta(minutes=30)


@dataclass(slots=True)
class StoryDraft:
    """尚未发布、但已经通过完整 Canon 校验的内存草稿。"""

    raw: dict[str, Any]
    canon: Canon
    expires_at: datetime


class StoryService:
    """串行发布 Canon，并向广场提供脱敏摘要。"""

    def __init__(self, canon_dir: Path = DEFAULT_CANON_DIR) -> None:
        self._canon_dir = canon_dir
        self._drafts: dict[str, StoryDraft] = {}
        self._publish_lock = asyncio.Lock()

    async def interview(
        self, *, conversation: list[dict[str, Any]], design_brief: dict[str, Any]
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

    async def create_draft(self, design_brief: dict[str, Any]) -> StoryDraftResponse:
        """由确认设计稿生成通过校验的限时发布草稿。"""
        errors = validate_confirmed_design_brief(design_brief)
        if errors:
            raise HTTPException(status_code=422, detail="；".join(errors))
        raw, canon = await generate_canon(confirmed_brief=design_brief)
        self._validate_campaign_id(canon.campaign_id)
        now = datetime.now(UTC)
        self._purge_expired(now)
        draft_id = secrets.token_urlsafe(24)
        draft = StoryDraft(raw=raw, canon=canon, expires_at=now + _DRAFT_TTL)
        self._drafts[draft_id] = draft
        return StoryDraftResponse(
            draft_id=draft_id,
            expires_at=draft.expires_at,
            story=self.summary(canon),
        )

    async def publish(self, draft_id: str) -> StorySummary:
        """重新校验限时草稿，原子写盘并立即登记到进程注册表。"""
        async with self._publish_lock:
            now = datetime.now(UTC)
            self._purge_expired(now)
            draft = self._drafts.get(draft_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="故事草稿不存在或已经过期")
            errors = [
                *validate_canon(draft.canon),
                *validate_authored_canon(draft.canon),
            ]
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail="Canon 发布前校验失败：" + "；".join(errors),
                )
            self._validate_campaign_id(draft.canon.campaign_id)
            self._canon_dir.mkdir(parents=True, exist_ok=True)
            target = self._canon_dir / f"{draft.canon.campaign_id}.json"
            if target.exists() or get_registry().get(draft.canon.campaign_id):
                raise HTTPException(
                    status_code=409, detail="剧本 ID 已存在，请重新生成"
                )

            temporary = self._canon_dir / (
                f".{draft.canon.campaign_id}.{secrets.token_hex(8)}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(draft.raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()

            get_registry().register(draft.canon)
            del self._drafts[draft_id]
            return self.summary(draft.canon)

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

    def _purge_expired(self, now: datetime) -> None:
        """在读写草稿时顺带清理已过期记录。"""
        expired = [
            key for key, draft in self._drafts.items() if draft.expires_at <= now
        ]
        for key in expired:
            del self._drafts[key]

    @staticmethod
    def _validate_campaign_id(campaign_id: str) -> None:
        """限制发布文件名为安全且稳定的 snake_case 标识。"""
        if not _CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
            raise HTTPException(
                status_code=422,
                detail="campaign_id 必须是 3–64 位 lowercase snake_case",
            )


story_service = StoryService()
