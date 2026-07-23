"""WebSocket 连接管理器 —— 维护 user_id → WebSocket 的映射"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """同时支持旧用户通道与多人房间通道。"""

    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.room_connections: dict[str, dict[str, list[WebSocket]]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, []).append(websocket)
        logger.info(
            "[ws] 用户 %s 已连接，当前连接数: %d",
            user_id,
            len(self.active_connections[user_id]),
        )

    def disconnect(self, user_id: str, websocket: WebSocket):
        conns = self.active_connections.get(user_id)
        if conns is None:
            return
        conns.remove(websocket)
        if not conns:
            del self.active_connections[user_id]
        logger.info("[ws] 用户 %s 已断开", user_id)

    async def send_json(self, user_id: str, data: dict[str, Any]):
        """向指定用户的所有连接发送 JSON 消息。"""
        conns = self.active_connections.get(user_id)
        if not conns:
            return
        text = json.dumps(data, ensure_ascii=False)
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception:
                logger.warning("[ws] 发送失败，user_id=%s", user_id, exc_info=True)

    async def send_text(self, user_id: str, message: str):
        """向指定用户的所有连接发送纯文本。"""
        conns = self.active_connections.get(user_id)
        if not conns:
            return
        for ws in conns:
            try:
                await ws.send_text(message)
            except Exception:
                logger.warning("[ws] 发送失败，user_id=%s", user_id, exc_info=True)

    async def connect_room(
        self, room_code: str, user_id: str, websocket: WebSocket
    ) -> None:
        """把一个浏览器连接登记到指定房间和用户。"""
        await websocket.accept()
        users = self.room_connections.setdefault(room_code, {})
        users.setdefault(user_id, []).append(websocket)
        logger.info(
            "[ws.room] 用户加入连接 | room_code=%s | user_id=%s | count=%d",
            room_code,
            user_id,
            len(users[user_id]),
        )

    def disconnect_room(
        self, room_code: str, user_id: str, websocket: WebSocket
    ) -> None:
        """移除一个房间连接，并清理空桶。"""
        users = self.room_connections.get(room_code)
        if users is None:
            return
        connections = users.get(user_id)
        if connections is None:
            return
        if websocket in connections:
            connections.remove(websocket)
        if not connections:
            users.pop(user_id, None)
        if not users:
            self.room_connections.pop(room_code, None)
        logger.info(
            "[ws.room] 用户断开连接 | room_code=%s | user_id=%s",
            room_code,
            user_id,
        )

    def room_user_connection_count(self, room_code: str, user_id: str) -> int:
        """返回用户在房间中的活跃连接数。"""
        return len(self.room_connections.get(room_code, {}).get(user_id, []))

    async def send_room_user(
        self, room_code: str, user_id: str, data: dict[str, Any]
    ) -> None:
        """向房间中的指定用户发送 JSON。"""
        connections = list(self.room_connections.get(room_code, {}).get(user_id, []))
        if not connections:
            return
        text = json.dumps(data, ensure_ascii=False)
        stale: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_text(text)
            except Exception:
                stale.append(websocket)
                logger.warning(
                    "[ws.room] 私发失败 | room_code=%s | user_id=%s",
                    room_code,
                    user_id,
                    exc_info=True,
                )
        for websocket in stale:
            self.disconnect_room(room_code, user_id, websocket)

    async def broadcast_room(self, room_code: str, data: dict[str, Any]) -> None:
        """向房间内所有在线成员广播 JSON。"""
        users = list(self.room_connections.get(room_code, {}).keys())
        for user_id in users:
            await self.send_room_user(room_code, user_id, data)


manager = ConnectionManager()
