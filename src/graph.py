"""
LangGraph 两阶段并行流程

Phase 1 (并行): message_analyze + strategy
  ↓ phase1_join (fan-in)
Phase 2 (并行, 依赖 Phase1 的 message_analysis): sales_stage + encouragement + userinfo + intent_analyzer
  ↓ phase2_join (fan-in)
Phase 3 (串行): sftb → wording → polishing

每阶段的 Send 并行分支通过 join 节点汇合（普通边 fan-in），
确保下游条件路由只触发一次，避免 InvalidUpdateError。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TypedDict

from src.common.utils.log_util import ensure_logging_config, get_elapsed_ms
from src.common.utils.writer import astream_agent_collect
from src.common.ws.ws_manager import manager as ws_manager

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ensure_logging_config()
logger = logging.getLogger(__name__)


def _format_log_fields(**kwargs: object) -> str:
    """将日志字段格式化为便于阅读的 key=value 形式。"""
    valid_items = [
        f"{key}={value}" for key, value in kwargs.items() if value is not None
    ]
    return " | " + " | ".join(valid_items) if valid_items else ""


def _log_step_start(step_name: str, **kwargs: object) -> float:
    """记录步骤开始日志，并返回计时起点。"""
    start_time = time.perf_counter()
    logger.info("[%s] 开始%s", step_name, _format_log_fields(**kwargs))
    return start_time


def _log_step_end(step_name: str, start_time: float, **kwargs: object) -> None:
    """记录步骤结束日志和耗时。"""
    logger.info(
        "[%s] 结束 | elapsed_ms=%s%s",
        step_name,
        get_elapsed_ms(start_time),
        _format_log_fields(**kwargs),
    )


class GraphState(TypedDict):
    """主图状态：存储用户输入和所有 Agent 的输出"""

    user_input: str
    thread_id: str
    user_id: str
    result: str


from src.common.example.example_agent import PROMPT_KEY, create_skills_find_agent


async def process(state: GraphState) -> dict:
    """SFTB 策略规划师节点：Phase2 全部完成后执行。"""
    start_time = _log_step_start(
        "process_sftb",
        thread_id=state["thread_id"],
    )

    try:
        agent = await create_skills_find_agent()
        result = await astream_agent_collect(
            agent,
            state["user_input"],
            thread_id="thread_123",
            node_name=PROMPT_KEY,
        )
        _log_step_end(
            "process_sftb",
            start_time,
            thread_id=state["thread_id"],
            output_length=len(result),
        )
        return {"result": result}
    except Exception:
        logger.exception(
            "[process_sftb] 异常%s",
            _format_log_fields(
                thread_id=state["thread_id"],
                elapsed_ms=get_elapsed_ms(start_time),
            ),
        )
        raise


from langgraph.graph import StateGraph, START, END


async def create_graph():
    graph = StateGraph(GraphState)
    graph.add_node("process", process)
    graph.set_entry_point("process")
    from langgraph.checkpoint.memory import MemorySaver
    return graph.compile(checkpointer=MemorySaver())


async def invoke(user_id: str | None = None, ) -> dict:
    graph = await create_graph()
    async for mode, chunk in graph.astream(
            {

                "user_input": "你有什么技能呢",
                "thread_id": "str",
                "user_id": "1234",
                "result": "str",
            },
            config={"configurable": {"thread_id": "thread_123"}},
            stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            status = chunk.get("status", "streaming")
            node = chunk.get("node", "")
            if user_id:
                if status == "start":
                    await ws_manager.send_json(
                        user_id, {"type": "node_start", "node": node}
                    )
                elif status == "end":
                    await ws_manager.send_json(
                        user_id, {"type": "node_end", "node": node}
                    )
                else:
                    await ws_manager.send_json(
                        user_id,
                        {
                            "type": "stream",
                            "node": node,
                            "content": chunk.get("chunk", ""),
                        },
                    )
            logger.debug(
                "[实时流] node=%s, status=%s, len=%d",
                node,
                status,
                len(chunk.get("chunk", "")),
            )
        elif mode == "values":
            result = chunk
    logger.info("[结果] %s", result)
    return result


if __name__ == '__main__':
    asyncio.run(invoke("1234"))
