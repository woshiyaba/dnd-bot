"""DM 智能体装配与调用（基于 ``langchain.agents.create_agent``，不依赖 deepagents）。

``create_agent`` 内置"工具调用循环"：把 DM 工具（骰子 + 知识库）与系统提示词绑给模型后，
模型可在一次调用里自行决定查规则/掷骰，再给出结论，无需我们手写循环。

对外提供两类调用：
- :func:`dm_complete_json` —— 决策类（突袭判定、怪物动作）：要求模型输出 JSON，
  用现有 :func:`extract_json_object` 防御式解析（不依赖具体厂商的结构化输出支持）。
- :func:`dm_narrate` —— 叙述类：流式把 token 经 ``get_stream_writer()`` 推给前端，
  复用与 ``graph.py`` 一致的 custom 事件通道。

智能体按系统提示词缓存复用（仿 ``example_agent`` 的 create-once 模式）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain.agents import create_agent

from src.common.utils.json_parser import extract_json_object
from src.common.utils.llm_util import create_chat_model
from src.common.utils.writer import StreamCollector
from src.dm.prompt import build_dm_system_prompt
from src.dm.tools import ALL_DM_TOOLS

logger = logging.getLogger(__name__)

_agent_lock = asyncio.Lock()
_cached_agent: Any | None = None
_cached_prompt: str | None = None


async def get_dm_agent() -> Any:
    """获取（并缓存）DM 智能体；仅当系统提示词变化（如知识库目录更新）时才重建。"""
    global _cached_agent, _cached_prompt

    system_prompt = build_dm_system_prompt()
    if _cached_agent is not None and _cached_prompt == system_prompt:
        return _cached_agent

    async with _agent_lock:
        if _cached_agent is not None and _cached_prompt == system_prompt:
            return _cached_agent
        _cached_agent = create_agent(
            create_chat_model(),  # 复用默认模型（qwen3.5-plus，DashScope 兼容）
            tools=ALL_DM_TOOLS,
            system_prompt=system_prompt,
        )
        _cached_prompt = system_prompt
        return _cached_agent


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


async def dm_complete_json(task: str) -> dict | None:
    """跑一轮 DM 决策（可掷骰/查规则），要求输出 JSON 并解析为字典。

    参数 task 为本次决策的完整任务描述（含情境与"请输出 JSON"的格式要求）。
    解析失败返回 None，由调用方回落到启发式。
    """
    agent = await get_dm_agent()
    result = await agent.ainvoke({"messages": [{"role": "user", "content": task}]})
    return extract_json_object(_last_text(result))


async def dm_narrate(task: str, *, node_name: str = "narrate") -> str:
    """跑一轮 DM 叙述，流式把文本 token 推给前端（custom 通道），并返回完整叙述文本。

    参数:
        task: 叙述任务描述（含本回合发生的结构化事件）。
        node_name: custom 事件里的节点名，前端据此归类；默认 ``"narrate"``。
    """
    agent = await get_dm_agent()
    collector = StreamCollector(node_name)
    collector.start()
    try:
        async for token, _meta in agent.astream(
            {"messages": [{"role": "user", "content": task}]},
            stream_mode="messages",
        ):
            content = getattr(token, "content", "")
            # 只推送模型的文本输出；工具调用分片的 content 为空，自动跳过
            if isinstance(content, str) and content:
                collector.push(content)
    finally:
        collector.finish()
    return collector.result
