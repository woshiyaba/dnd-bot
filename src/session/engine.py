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

from src.model.dm_state import init_story, load_party
from src.session.graph import build_session_graph, reset_session_dice
from src.story.loader import get_registry

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
              "campaign_id": "whispers_bell_tower",  # 本局 canon（注册表 key）；给定即按剧本骨架开局
              "dm_mode": "llm" ,                      # 启用 LLM 版 DM
              "random_seed": int,                     # 可复现随机源（探索暗骰 + 战斗）
              "scene": { ...WorldScene... },           # 无 canon 时的初始世界场景（退化纯对话）
              "party": [ {type:"player", controller, card}, ... ],  # 玩家队伍
            }

        给定 ``campaign_id`` 且注册表中存在该 canon 时，按起始拍 ``entry_state`` 初始化故事进度与场景；
        否则退化为旧的「纯对话」开局（无故事主轴）。
        """
        reset_session_dice(scene_context)
        dm_mode = scene_context.get("dm_mode", "llm")
        campaign_id = scene_context.get("campaign_id")
        canon = get_registry().get(campaign_id) if campaign_id else None

        if canon is not None:
            # 按剧本骨架开局：起始拍 entry_state → 初始 story + scene
            story, scene = init_story(canon)
            scene["dm_mode"] = dm_mode
            if scene.get("random_seed") is None:
                scene["random_seed"] = scene_context.get("random_seed")
        else:
            # 无 canon：退化为纯对话开局
            if campaign_id:
                logger.warning("[session] campaign_id «%s» 未在注册表中找到，退化为纯对话开局", campaign_id)
            scene = dict(scene_context.get("scene", {}))
            scene.setdefault("dm_mode", dm_mode)
            scene.setdefault("random_seed", scene_context.get("random_seed"))
            # 战利品表登记进场景，触发战斗时带给战斗子图（settle 据此发放）
            if "loot_table" in scene_context:
                scene.setdefault("loot_table", scene_context["loot_table"])
            story = {}

        init_state = {
            "messages": [],
            "user_input": opening,
            "user_id": scene_context.get("user_id"),
            "room_id": room_id,
            "scene": scene,
            "party": load_party(scene_context),
            "campaign_log": [],
            "next": "wait",
            "campaign_id": campaign_id or "",
            "story": story,
            "story_status": "ongoing",
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

        # 整局结束（到达并叙述完结局拍）
        if result.get("story_status") == "finished":
            story = result.get("story") or {}
            logger.info("[session] 房间=%s 整局结束 | 结局拍=%s", room_id, story.get("current_beat_id"))
            return {
                "status": "finished",
                "room_id": room_id,
                "say": say,
                "ending_beat_id": story.get("current_beat_id"),
                "last_combat": result.get("last_combat"),
                "state": result,
            }

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
