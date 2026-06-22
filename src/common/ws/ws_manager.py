"""WebSocket 连接管理器 —— 维护 user_id → WebSocket 的映射"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """支持一个用户持有多个 WebSocket 连接。"""

    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.setdefault(user_id, []).append(websocket)
        logger.info("[ws] 用户 %s 已连接，当前连接数: %d", user_id, len(self.active_connections[user_id]))

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


manager = ConnectionManager()
