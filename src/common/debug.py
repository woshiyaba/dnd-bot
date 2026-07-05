"""LangGraph 调试模式工具。"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi.encoders import jsonable_encoder
from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)

_enabled = False
_system_prompts: dict[str, str] = {}


def configure_debug(enabled: bool) -> None:
    """配置进程级调试模式开关。"""
    global _enabled
    _enabled = enabled
    if enabled:
        logger.info("[debug] LangGraph 调试模式已开启")


def is_debug_enabled() -> bool:
    """返回调试模式是否启用。"""
    return _enabled or os.getenv("DND_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def register_system_prompt(
    node_name: str, system_prompt: str, *, aliases: tuple[str, ...] = ()
) -> None:
    """登记节点或智能体使用的系统提示词，供 debug 事件附带展示。"""
    if not system_prompt:
        return
    for name in (node_name, *aliases):
        if name:
            _system_prompts[name] = system_prompt


def system_prompt_for(node_name: str) -> str | None:
    """读取节点对应的系统提示词；无 LLM 的规则节点返回 None。"""
    return _system_prompts.get(node_name)


def normalize_debug_event(
    event: dict[str, Any], *, namespace: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """把 LangGraph debug 事件规整为前端协议。"""
    event_type = event.get("type")
    if event_type not in {"task", "task_result"}:
        return None

    payload = event.get("payload") or {}
    node = str(payload.get("name") or "")
    if not node:
        return None

    normalized = {
        "type": "debug_node",
        "event": "input" if event_type == "task" else "output",
        "node": node,
        "path": _node_path(namespace, node),
        "namespace": [_strip_namespace_part(part) for part in namespace],
        "task_id": str(payload.get("id") or ""),
        "step": event.get("step"),
        "timestamp": event.get("timestamp"),
        "system_prompt": system_prompt_for(node),
    }
    if event_type == "task":
        normalized["input"] = to_debug_value(payload.get("input"))
        normalized["triggers"] = to_debug_value(payload.get("triggers") or [])
    else:
        normalized["output"] = to_debug_value(payload.get("result"))
        normalized["error"] = to_debug_value(payload.get("error"))
        normalized["interrupts"] = to_debug_value(payload.get("interrupts") or [])
    return normalized


def emit_custom_debug_event(event: dict[str, Any]) -> None:
    """在图节点内部把手动采集的 debug 事件写入 custom 流。"""
    if not is_debug_enabled():
        return
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"status": "debug", "debug": event})


def to_debug_value(value: Any) -> Any:
    """把任意领域对象转成 JSON 安全结构，失败时保留字符串表示。"""
    try:
        return jsonable_encoder(value)
    except Exception:  # noqa: BLE001 - debug 不能影响主流程
        try:
            return str(value)
        except Exception:
            return "<unserializable>"


def _node_path(namespace: tuple[str, ...], node: str) -> str:
    parts = [_strip_namespace_part(part) for part in namespace]
    parts.append(node)
    return " / ".join(part for part in parts if part)


def _strip_namespace_part(value: str) -> str:
    return value.split(":", 1)[0]
