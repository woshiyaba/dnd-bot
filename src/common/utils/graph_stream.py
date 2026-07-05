"""LangGraph 流式执行工具。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from src.common.debug import is_debug_enabled, normalize_debug_event

GraphStreamSink = Callable[[dict[str, Any]], Awaitable[None]]


async def astream_graph_values(
    graph: Any,
    graph_input: Any,
    *,
    config: dict,
    event_sink: GraphStreamSink | None = None,
) -> dict:
    """流式运行 LangGraph，转交 custom 事件并返回最后一个 values 状态。

    本工具只处理图层协议：
      - custom: 原样交给 event_sink
      - values: 保存最后一次状态快照

    WebSocket 事件映射、JSON 编码和业务 payload 解释都留给调用方。
    """
    result: dict | None = None
    stream_modes = ["custom", "values"]
    if is_debug_enabled():
        stream_modes.append("debug")

    async for item in graph.astream(
        graph_input,
        config=config,
        stream_mode=stream_modes,
        subgraphs=is_debug_enabled(),
    ):
        namespace: tuple[str, ...] = ()
        if is_debug_enabled() and len(item) == 3:
            namespace, mode, chunk = item
        else:
            mode, chunk = item

        if mode == "custom":
            if event_sink is not None:
                await event_sink(chunk)
        elif mode == "values":
            result = chunk
        elif mode == "debug" and event_sink is not None:
            debug_event = normalize_debug_event(chunk, namespace=namespace)
            if debug_event is not None:
                await event_sink(debug_event)

    if result is None:
        raise RuntimeError("[graph_stream] 图流式执行未返回 values 状态")
    return result
