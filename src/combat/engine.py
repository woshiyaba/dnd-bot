"""战斗引擎对外门面（CombatEngine）。

包住战斗子图，按「房间」用唯一 thread_id 驱动一场可中断、可持久化的战斗：

- `start_combat(room_id, scene_context)`：跑到第一个需要玩家骰子的中断点（或直接结束）。
- `submit(room_id, resume_value)`：用 `Command(resume=resume_value)` 恢复，跑到下一个中断点。
- 返回统一负载：`{"status": "interrupted", "interrupt": 请求, ...}` 或 `{"status": "finished", "result": ..., "state": ...}`。

中断请求里的 `directed_to.user_id` 给上层（app.py / ws_manager）用来把「该谁掷什么」推给正确的玩家。
对接细节见 docs/战斗/03-中断交互协议.md 第 5 节。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

from src.combat.graph import build_combat_graph
from src.model.enums import CombatOutcome

logger = logging.getLogger(__name__)


def room_thread_id(room_id: str) -> str:
    """每场战斗一个唯一 thread_id（建议 `combat:{room_id}`），用于 checkpointer 存档。"""
    return f"combat:{room_id}"


class CombatEngine:
    """战斗子图门面。一个实例可服务多个房间（各自独立 thread_id）。"""

    def __init__(self, checkpointer: Any | None = None):
        self._graph = build_combat_graph(checkpointer)

    # ---- 对外主流程 ----
    async def start_combat(self, room_id: str, scene_context: dict) -> dict:
        """开一场新战斗，跑到第一个玩家中断点或结束。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        result = await self._graph.ainvoke(
            {"scene_context": scene_context}, config=config
        )
        return self._interpret(room_id, result, config)

    async def submit(self, room_id: str, resume_value: Any) -> dict:
        """玩家报骰/选择后恢复战斗，跑到下一个中断点或结束。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        result = await self._graph.ainvoke(Command(resume=resume_value), config=config)
        return self._interpret(room_id, result, config)

    async def current_state(self, room_id: str) -> dict | None:
        """读取某房间当前 CombatState 快照（断线重连/旁观用）。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        snapshot = await self._graph.aget_state(config)
        return snapshot.values if snapshot else None

    # ---- 内部：把 ainvoke 结果归一成统一负载 ----
    def _interpret(self, room_id: str, result: dict, config: dict) -> dict:
        interrupts = result.get("__interrupt__")
        if interrupts:
            request = interrupts[0].value
            logger.info(
                "[combat] 房间=%s 等待中断 类型=%s 面向=%s",
                room_id,
                request.get("interrupt_type"),
                request.get("directed_to"),
            )
            return {
                "status": "interrupted",
                "room_id": room_id,
                "interrupt": request,
                "outcome": _outcome_value(result),
            }

        logger.info(
            "[combat] 房间=%s 战斗结束 结果=%s", room_id, _outcome_value(result)
        )
        return {
            "status": "finished",
            "room_id": room_id,
            "result": _outcome_value(result),
            "state": result,
        }


def _outcome_value(state: dict) -> str | None:
    """从 state 里取战斗结果的字符串值（容忍枚举或已是字符串）。"""
    value = state.get("outcome")
    if isinstance(value, CombatOutcome):
        return value.value
    return value
