"""会话引擎对外门面（SessionEngine）。

包住会话主图，按「房间/局」用唯一 thread_id 驱动一整局可中断、可持久化的冒险：

- ``start_session(room_id, scene_context, opening)``：开局——载入队伍与世界场景，跑 DM 的开场。
- ``message(room_id, user_input)``：玩家说一句话/做一件事，推进一个 DM 回合。
- ``submit(room_id, resume_value)``：玩家报骰/选择后恢复（明检定、或战斗内的攻击/先攻骰）。
- ``current_state(room_id)``：读当前 DMState 快照（断线重连/旁观）。

统一负载：
- ``{"status":"interrupted", "interrupt": 请求, ...}``：等玩家报骰（DM 检定或战斗骰，协议同 docs/战斗/03）。
- ``{"status":"awaiting_input", "say": DM最近的话, ...}``：本回合讲完，等玩家下一条消息。

中断请求里的 ``directed_to.user_id`` 给上层（app.py / ws_manager）用来路由「该谁掷什么」。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command

from src.model.dm_state import load_party
from src.session.graph import build_session_graph, reset_session_dice

logger = logging.getLogger(__name__)


def room_thread_id(room_id: str) -> str:
    """每局一个唯一 thread_id（建议 ``session:{room_id}``），用于 checkpointer 存档。"""
    return f"session:{room_id}"


class SessionEngine:
    """会话主图门面。一个实例可服务多个房间（各自独立 thread_id）。"""

    def __init__(self, checkpointer: Any | None = None):
        """checkpointer 缺省 MemorySaver（单进程）；多人/重启传持久化版。"""
        self._graph = build_session_graph(checkpointer)

    # ---- 对外主流程 ----
    async def start_session(self, room_id: str, scene_context: dict, *, opening: str = "") -> dict:
        """开一局新冒险：载入队伍/场景，跑 DM 开场（或处理 opening 这步输入）。

        scene_context 格式::

            {
              "dm_mode": "llm" ,   # 启用 LLM 版 DM
              "random_seed": int,               # 可复现随机源（探索暗骰 + 战斗）
              "scene": { ...WorldScene... },     # 初始世界场景
              "party": [ {type:"player", controller, card}, ... ],  # 玩家队伍
            }
        """
        reset_session_dice(scene_context)
        scene = dict(scene_context.get("scene", {}))
        scene.setdefault("dm_mode", scene_context.get("dm_mode", "llm"))
        scene.setdefault("random_seed", scene_context.get("random_seed"))
        # 战利品表登记进场景，触发战斗时带给战斗子图（settle 据此发放）
        if "loot_table" in scene_context:
            scene.setdefault("loot_table", scene_context["loot_table"])

        init_state = {
            "messages": [],
            "user_input": opening,
            "user_id": scene_context.get("user_id"),
            "room_id": room_id,
            "scene": scene,
            "party": load_party(scene_context),
            "campaign_log": [],
            "next": "wait",
        }
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        result = await self._graph.ainvoke(init_state, config=config)
        return self._interpret(room_id, result)

    async def message(self, room_id: str, user_input: str) -> dict:
        """玩家说一句话/做一件事，推进一个 DM 回合（沿用已存档的世界状态）。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        result = await self._graph.ainvoke({"user_input": user_input}, config=config)
        return self._interpret(room_id, result)

    async def submit(self, room_id: str, resume_value: Any) -> dict:
        """玩家报骰/选择后恢复（DM 明检定，或战斗内的攻击/先攻/豁免骰）。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        result = await self._graph.ainvoke(Command(resume=resume_value), config=config)
        return self._interpret(room_id, result)

    async def current_state(self, room_id: str) -> dict | None:
        """读取某房间当前 DMState 快照（断线重连/旁观用）。"""
        config = {"configurable": {"thread_id": room_thread_id(room_id)}}
        snapshot = await self._graph.aget_state(config)
        return snapshot.values if snapshot else None

    # ---- 内部：把 ainvoke 结果归一成统一负载 ----
    def _interpret(self, room_id: str, result: dict) -> dict:
        interrupts = result.get("__interrupt__")
        if interrupts:
            request = interrupts[0].value
            logger.info(
                "[session] 房间=%s 等待中断 类型=%s 面向=%s",
                room_id, request.get("interrupt_type"), request.get("directed_to"),
            )
            return {
                "status": "interrupted",
                "room_id": room_id,
                "interrupt": request,
                "state": result,
            }

        say = _last_dm_say(result)
        logger.info("[session] 房间=%s 回合结束，等待玩家输入", room_id)
        return {
            "status": "awaiting_input",
            "room_id": room_id,
            "say": say,
            "last_check": result.get("last_check"),
            "last_combat": result.get("last_combat"),
            "state": result,
        }


def _last_dm_say(state: dict) -> str | None:
    """取对话历史里 DM 最近说的一句话。"""
    for msg in reversed(state.get("messages", []) or []):
        if msg.get("role") == "dm":
            return msg.get("content")
    return None
