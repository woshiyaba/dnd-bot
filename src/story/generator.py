"""使用真实 LLM 执行故事访谈、Canon 编译和校验修复。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import create_chat_model
from src.model.canon import Canon, validate_authored_canon, validate_canon
from src.schemas.story import StoryInterviewResponse
from src.story.prompt import (
    build_canon_authoring_prompt,
    build_canon_repair_prompt,
    build_story_interview_prompt,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANON_DIR = PROJECT_ROOT / "canon"
REFERENCE_CANON_PATHS = (
    CANON_DIR / "prodigal_return_quest.json",
    CANON_DIR / "whispers_bell_tower.json",
)
DEFAULT_STORY_GENERATION_MODEL = "deepseek-v4-pro"
STORY_GENERATION_MODEL = os.getenv(
    "STORY_GENERATION_MODEL", DEFAULT_STORY_GENERATION_MODEL
)
MAX_REPAIR_ATTEMPTS = 2

_model: Any | None = None
_model_lock = asyncio.Lock()


class StoryGenerationError(RuntimeError):
    """真实 LLM 未能返回可用的故事结构或 Canon。"""


async def _get_model() -> Any:
    """创建并缓存故事编剧使用的真实聊天模型。"""
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is None:
            _model = create_chat_model(
                model=STORY_GENERATION_MODEL,
                enable_search=False,
            )
        return _model


def _load_reference_canons() -> list[dict[str, Any]]:
    """每次编译重新读取内置 Canon，使故事框架改动立即进入生成上下文。"""
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in REFERENCE_CANON_PATHS
    ]


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


async def _complete_json(prompt: str, *, stage: str) -> dict[str, Any]:
    """调用真实 LLM 并提取 JSON；任何失败都显式抛错。"""
    try:
        response = await (await _get_model()).ainvoke(prompt)
    except Exception as exc:
        logger.exception("[story_generator] LLM 调用失败 | stage=%s", stage)
        raise StoryGenerationError(f"故事 {stage} 的 LLM 调用失败：{exc}") from exc
    parsed = extract_json_object(_message_text(response))
    if parsed is None:
        raise StoryGenerationError(f"故事 {stage} 的 LLM 输出不是可解析的 JSON 对象")
    return parsed


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
    )
    try:
        return StoryInterviewResponse.model_validate(raw)
    except ValidationError as exc:
        raise StoryGenerationError(f"故事访谈输出结构不合法：{exc}") from exc


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
        )

    raise AssertionError("Canon 修复循环未按预期结束")
