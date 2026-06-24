"""战斗引擎对外门面（CombatEngine）。

包住战斗子图，按「房间」用唯一 thread_id 驱动一场可中断、可持久化的战斗：

- `开始战斗(房间id, 场景上下文)`：跑到第一个需要玩家骰子的中断点（或直接结束）。
- `提交(房间id, 恢复值)`：用 `Command(resume=恢复值)` 恢复，跑到下一个中断点。
- 返回统一负载：`{"状态": "中断", "中断": 请求, ...}` 或 `{"状态": "结束", "结果": ..., "state": ...}`。

中断请求里的 `面向.user_id` 给上层（app.py / ws_manager）用来把「该谁掷什么」推给正确的玩家。
对接细节见 docs/战斗/03-中断交互协议.md 第 5 节。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

from src.combat.graph import build_combat_graph
from src.model.enums import 战斗结果

logger = logging.getLogger(__name__)


def 房间线程id(房间id: str) -> str:
    """每场战斗一个唯一 thread_id（建议 `combat:{房间id}`），用于 checkpointer 存档。"""
    return f"combat:{房间id}"


class CombatEngine:
    """战斗子图门面。一个实例可服务多个房间（各自独立 thread_id）。"""

    def __init__(self, checkpointer: Any | None = None):
        self._graph = build_combat_graph(checkpointer)

    # ---- 对外主流程 ----
    async def 开始战斗(self, 房间id: str, 场景上下文: dict) -> dict:
        """开一场新战斗，跑到第一个玩家中断点或结束。"""
        config = {"configurable": {"thread_id": 房间线程id(房间id)}}
        结果 = await self._graph.ainvoke({"场景上下文": 场景上下文}, config=config)
        return self._解读(房间id, 结果, config)

    async def 提交(self, 房间id: str, 恢复值: Any) -> dict:
        """玩家报骰/选择后恢复战斗，跑到下一个中断点或结束。"""
        config = {"configurable": {"thread_id": 房间线程id(房间id)}}
        结果 = await self._graph.ainvoke(Command(resume=恢复值), config=config)
        return self._解读(房间id, 结果, config)

    async def 当前状态(self, 房间id: str) -> dict | None:
        """读取某房间当前 CombatState 快照（断线重连/旁观用）。"""
        config = {"configurable": {"thread_id": 房间线程id(房间id)}}
        快照 = await self._graph.aget_state(config)
        return 快照.values if 快照 else None

    # ---- 内部：把 ainvoke 结果归一成统一负载 ----
    def _解读(self, 房间id: str, 结果: dict, config: dict) -> dict:
        中断列表 = 结果.get("__interrupt__")
        if 中断列表:
            请求 = 中断列表[0].value
            logger.info(
                "[combat] 房间=%s 等待中断 类型=%s 面向=%s",
                房间id, 请求.get("中断类型"), 请求.get("面向"),
            )
            return {
                "状态": "中断",
                "房间id": 房间id,
                "中断": 请求,
                "战斗结果": _取结果值(结果),
            }

        logger.info("[combat] 房间=%s 战斗结束 结果=%s", 房间id, _取结果值(结果))
        return {
            "状态": "结束",
            "房间id": 房间id,
            "结果": _取结果值(结果),
            "state": 结果,
        }


def _取结果值(state: dict) -> str | None:
    值 = state.get("战斗结果")
    if isinstance(值, 战斗结果):
        return 值.value
    return 值
