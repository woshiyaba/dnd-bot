"""DM 决策智能体与无工具叙述模型的装配入口。

``create_agent`` 内置"工具调用循环"：把 DM 工具（骰子 + 知识库）与系统提示词绑给模型后，
决策模型可自行查规则/掷骰，再给出结论。叙述则直接调用聊天模型，绝不挂载工具。

对外提供两类调用：
- :func:`dm_complete_json` —— 决策类（突袭判定、怪物动作）：要求模型输出 JSON，
  用现有 :func:`extract_json_object` 防御式解析（不依赖具体厂商的结构化输出支持）。
- :func:`dm_narrate` —— 叙述类：流式把 token 经 ``StreamCollector`` 推给前端，
  复用与 ``graph.py`` 一致的 custom 事件通道。

决策智能体按模型、系统提示词和工具集合缓存；聊天模型由中央注册表缓存。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain.agents import create_agent

from src.common.debug import register_system_prompt
from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import get_chat_model
from src.common.utils.writer import StreamCollector
from src.dm.prompt import build_dm_system_prompt
from src.dm.tools import ALL_DM_TOOLS

logger = logging.getLogger(__name__)

_agent_lock = asyncio.Lock()
_cached_agents: dict[tuple[str, str, tuple[str, ...]], Any] = {}
_KB_TOOL_NAMES = {"kb_search", "kb_read"}
_KNOWLEDGE_LOG_LIMIT = 12000


async def get_dm_agent(model_name: str) -> Any:
    """按模型、系统提示词与工具集合获取并缓存 DM 决策智能体。"""
    system_prompt = build_dm_system_prompt()
    _register_dm_prompt(system_prompt)
    tool_names = tuple(str(getattr(tool, "name", tool)) for tool in ALL_DM_TOOLS)
    cache_key = (model_name, system_prompt, tool_names)
    cached = _cached_agents.get(cache_key)
    if cached is not None:
        return cached

    async with _agent_lock:
        cached = _cached_agents.get(cache_key)
        if cached is not None:
            return cached
        agent = create_agent(
            get_chat_model(model_name),
            tools=ALL_DM_TOOLS,
            system_prompt=system_prompt,
        )
        _cached_agents[cache_key] = agent
        return agent


def _register_dm_prompt(system_prompt: str) -> None:
    """登记所有会走 DM 智能体的图节点系统提示词。"""
    register_system_prompt(
        "dm",
        system_prompt,
        aliases=(
            "dm_decide",
            "evaluate_advancement",
            "final_narrate_turn",
            "judge_surprise",
            "declare_action",
            "narrate",
        ),
    )


def _last_text(result: dict) -> str:
    """从 agent 结果里取最后一条消息的文本内容（兼容 content 为分段列表的情况）。"""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return ""
    content = getattr(messages[-1], "content", "")
    if isinstance(content, str):
        return content
    # 某些模型把内容拆成分段列表，拼接其中的文本片段
    parts = [
        seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in content
    ]
    return "".join(parts)


def _log_text(value: Any) -> str:
    """把工具结果压成适合日志的一段文本，超长时截断避免刷屏。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > _KNOWLEDGE_LOG_LIMIT:
        return f"{text[:_KNOWLEDGE_LOG_LIMIT]}...（已截断，总长度 {len(text)}）"
    return text


def _tool_call_name_by_id(messages: list[Any]) -> dict[str, str]:
    """从 AI 消息的 tool_calls 中建立 tool_call_id 到工具名的映射。"""
    names: dict[str, str] = {}
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            name = call.get("name")
            if call_id and name:
                names[str(call_id)] = str(name)
    return names


def _log_knowledge_hits(result: dict, *, source: str) -> None:
    """打印 DM agent 本轮命中的 knowledge 工具结果。"""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return

    names_by_id = _tool_call_name_by_id(messages)
    hit_count = 0
    for message in messages:
        message_type = getattr(message, "type", "")
        if message_type != "tool":
            continue
        tool_call_id = str(getattr(message, "tool_call_id", "") or "")
        tool_name = (
            getattr(message, "name", None) or names_by_id.get(tool_call_id) or ""
        )
        if tool_name not in _KB_TOOL_NAMES:
            continue
        hit_count += 1
        logger.info(
            "[dm_agent] knowledge 命中 #%d | source=%s | tool=%s | content=%s",
            hit_count,
            source,
            tool_name,
            _log_text(getattr(message, "content", "")),
        )


async def dm_complete_json(task: str, *, model_name: str) -> dict | None:
    """跑一轮 DM 决策（可掷骰/查规则），要求输出 JSON 并解析为字典。

    参数 task 为本次决策的完整任务描述（含情境与"请输出 JSON"的格式要求）。
    解析失败返回 None；调用方必须显式失败，不允许回落到模拟 DM。
    """
    agent = await get_dm_agent(model_name)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
    _log_knowledge_hits(result, source="complete_json")
    return extract_json_object(_last_text(result))


async def dm_narrate(
    task: str,
    *,
    model_name: str,
    node_name: str | None = "narrate",
) -> str:
    """跑一轮 DM 叙述，流式把文本 token 推给前端（custom 通道），并返回完整叙述文本。

    参数:
        task: 叙述任务描述（含本回合发生的结构化事件）。
        model_name: 已登记的 ``供应商/模型 ID`` 复合名。
        node_name: custom 事件里的节点名，前端据此归类；默认 ``"narrate"``。
    """
    system_prompt = build_dm_system_prompt()
    _register_dm_prompt(system_prompt)
    model = get_chat_model(model_name)
    collector = StreamCollector(node_name)
    collector.start()
    try:
        async for token in model.astream(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
        ):
            content = getattr(token, "content", "")
            if isinstance(content, str) and content:
                collector.push(content)
    finally:
        collector.finish()
    return collector.result
