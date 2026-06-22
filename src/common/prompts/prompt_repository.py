from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv

from src.common.utils.mysql_util import fetch_one

load_dotenv()

DEFAULT_PROMPT_CACHE_TTL_SECONDS = 30


@dataclass
class _CachedPrompt:
    content: str
    expires_at: float


_cache: dict[str, _CachedPrompt] = {}
_lock = asyncio.Lock()


def _get_cache_ttl_seconds() -> int:
    raw_ttl = os.getenv(
        "PROMPT_CACHE_TTL_SECONDS",
        str(DEFAULT_PROMPT_CACHE_TTL_SECONDS),
    )
    try:
        ttl = int(raw_ttl)
    except ValueError as exc:
        raise RuntimeError(f"PROMPT_CACHE_TTL_SECONDS 必须是整数，当前值：{raw_ttl}") from exc
    return max(ttl, 0)


def _get_cached_prompt(prompt_key: str, now: float) -> str | None:
    cached = _cache.get(prompt_key)
    if cached and cached.expires_at > now:
        return cached.content
    return None


async def _load_prompt_from_db(prompt_key: str) -> str:
    row = await fetch_one(
        """
        SELECT prompt_content
        FROM agent_system_prompts
        WHERE prompt_key = %s AND enabled = 1
        LIMIT 1
        """,
        (prompt_key,),
    )
    if not row:
        raise LookupError(f"未找到启用的系统提示词：{prompt_key}")

    prompt_content = str(row.get("prompt_content") or "").strip()
    if not prompt_content:
        raise ValueError(f"系统提示词内容为空：{prompt_key}")
    return prompt_content


async def get_system_prompt(prompt_key: str) -> str:
    """按 key 获取系统提示词，支持短 TTL 缓存。"""
    normalized_key = prompt_key.strip()
    if not normalized_key:
        raise ValueError("prompt_key 不能为空")

    now = time.monotonic()
    cached = _get_cached_prompt(normalized_key, now)
    if cached is not None:
        return cached

    # 同一时刻多个请求缓存失效时，只让一个请求查库。
    async with _lock:
        now = time.monotonic()
        cached = _get_cached_prompt(normalized_key, now)
        if cached is not None:
            return cached

        prompt_content = await _load_prompt_from_db(normalized_key)
        ttl = _get_cache_ttl_seconds()
        if ttl > 0:
            _cache[normalized_key] = _CachedPrompt(
                content=prompt_content,
                expires_at=time.monotonic() + ttl,
            )
        else:
            _cache.pop(normalized_key, None)
        return prompt_content


def clear_prompt_cache(prompt_key: str | None = None) -> None:
    """清空提示词缓存，便于测试或后续管理接口主动刷新。"""
    if prompt_key is None:
        _cache.clear()
        return
    _cache.pop(prompt_key.strip(), None)
