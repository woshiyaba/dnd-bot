from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer


class StreamCollector:
    """流式输出收集器，管理节点生命周期并推送状态事件。

    将"流式推送"与"最终结果收集"分离：
      - push()      → 推送所有 token（含思考过程）到前端
      - push_result / reset_result → 仅追踪 agent 最后一轮 AI 输出

    node_name 非空时获取 stream_writer 并推送 custom 事件（图节点场景）；
    为 None 时仅收集 chunk（独立调用场景）。

    推送的 custom 事件格式：
      - start:      {"node": "xxx", "status": "start"}
      - streaming:  {"node": "xxx", "status": "streaming", "chunk": "..."}
      - end:        {"node": "xxx", "status": "end"}
    """

    def __init__(self, node_name: str | None = None):
        self._node_name = node_name
        self._chunks: list[str] = []
        self._result: str | None = None
        self._writer = get_stream_writer() if node_name else None

    def start(self):
        """推送节点开始事件，应在流式输出前调用。"""
        if self._writer:
            self._writer({"node": self._node_name, "status": "start"})

    def push(self, content: str):
        if not content:
            return
        self._chunks.append(content)
        if self._writer:
            self._writer({
                "node": self._node_name,
                "status": "streaming",
                "chunk": content,
            })

    def finish(self):
        """推送节点结束事件，应在流式输出完成后调用。"""
        if self._writer:
            self._writer({"node": self._node_name, "status": "end"})

    def set_result(self, content: str):
        """记录 updates 中提取的最终 AI 输出，每次覆盖写入以保留最新一轮。"""
        self._result = content

    @property
    def result(self) -> str:
        """优先返回 updates 提取的最终结果，兜底返回流式 token 拼接。"""
        if self._result is not None:
            return self._result
        return "".join(self._chunks)


def _extract_final_content(chunk) -> str | None:
    """从 updates 事件的 data 中提取最终 AIMessage.content。

    遍历 data 各节点的 messages 列表，找最后一条满足条件的 AI 消息：
      - type == "ai"
      - content 非空
      - 无 tool_calls（说明是最终输出而非中间调用）
    """
    candidate: str | None = None
    for node_name, data in chunk["data"].items():
        if node_name == "model":
            candidate = data["messages"][-1].content
    return candidate


def stream_agent_collect(
    agent: Any,
    content: str,
    thread_id: str,
    node_name: str | None = None,
) -> str:
    """流式调用 agent 并收集完整结果（同步版）。

    同时订阅 messages（流式推送思考过程）和 updates（提取最终结果）。
    node_name 非空时同时推送 start/streaming/end 状态事件。
    """
    sc = StreamCollector(node_name)
    sc.start()
    try:
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": content}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
            version="v2",
        ):
            if chunk["type"] == "messages":
                token, _metadata = chunk["data"]
                sc.push(token.content)
            elif chunk["type"] == "updates":
                final = _extract_final_content(chunk)
                if final:
                    sc.set_result(final)
    finally:
        sc.finish()
    return sc.result


async def astream_agent_collect(
    agent: Any,
    content: str,
    thread_id: str,
    node_name: str | None = None,
) -> str:
    """流式调用 agent 并收集完整结果（异步版）。

    同时订阅 messages（流式推送思考过程）和 updates（提取最终结果）。
    node_name 非空时同时推送 start/streaming/end 状态事件。
    """
    sc = StreamCollector(node_name)
    sc.start()
    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": content}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
            version="v2",
        ):
            if chunk["type"] == "messages":
                token, _metadata = chunk["data"]
                sc.push(token.content)
            elif chunk["type"] == "updates":
                final = _extract_final_content(chunk)
                if final:
                    sc.set_result(final)
    finally:
        sc.finish()
    return sc.result


async def agent_collect(
    agent: Any,
    content: str,
    thread_id: str,
    node_name: str | None = None,
) -> str:
    """流式调用 agent 并收集完整结果（异步版）。

    同时订阅 messages（流式推送思考过程）和 updates（提取最终结果）。
    node_name 非空时同时推送 start/streaming/end 状态事件。
    """
    agent.invoke(
        {"messages": [{"role": "user", "content": content}]},
        config={"configurable": {"thread_id": thread_id}}
    )
    sc = StreamCollector(node_name)
    sc.start()
    try:
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": content}]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
            version="v2",
        ):
            if chunk["type"] == "messages":
                token, _metadata = chunk["data"]
                sc.push(token.content)
            elif chunk["type"] == "updates":
                final = _extract_final_content(chunk)
                if final:
                    sc.set_result(final)
    finally:
        sc.finish()
    return sc.result
