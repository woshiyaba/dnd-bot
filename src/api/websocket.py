"""多人房间 WebSocket 事件通道。"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.common.ws.ws_manager import manager as ws_manager
from src.schemas.room import RoomEvent
from src.services.room_service import room_service
from src.services.session_service import session_service

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{user_id}")
async def agent_websocket(websocket: WebSocket, user_id: str) -> None:
    """保留通用 ``/invoke`` 图使用的用户级流式通道。"""
    await ws_manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, websocket)


@router.websocket("/ws/rooms/{room_code}")
async def room_websocket(websocket: WebSocket, room_code: str, token: str) -> None:
    """建立经过房间令牌校验的多人事件连接。"""
    try:
        room, member = room_service.authenticate(room_code, token)
    except Exception:
        await websocket.close(code=4401, reason="房间身份无效")
        return
    await ws_manager.connect_room(room.room_code, member.user_id, websocket)
    await room_service.set_online(room, member, True)
    await _broadcast_room(room)
    payload = await session_service.current_payload(room)
    if payload is not None:
        view = session_service.session_view(room, member, payload)
        await ws_manager.send_room_user(
            room.room_code,
            member.user_id,
            RoomEvent(
                type="session_updated",
                room_code=room.room_code,
                revision=room.revision,
                payload={"session": view.model_dump()},
            ).model_dump(),
        )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect_room(room.room_code, member.user_id, websocket)
        if ws_manager.room_user_connection_count(room.room_code, member.user_id) == 0:
            await room_service.set_online(room, member, False)
            await _broadcast_room(room)


async def _broadcast_room(room) -> None:
    """广播最新大厅/在线状态。"""
    lobby = room_service.lobby_view(room)
    await ws_manager.broadcast_room(
        room.room_code,
        RoomEvent(
            type="room_updated",
            room_code=room.room_code,
            revision=room.revision,
            payload={"room": lobby.model_dump()},
        ).model_dump(),
    )
