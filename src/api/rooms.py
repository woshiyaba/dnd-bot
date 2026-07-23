"""匿名多人房间与大厅路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from src.api.dependencies import require_room_member
from src.common.ws.ws_manager import manager as ws_manager
from src.schemas.room import (
    CharacterOption,
    CreateRoomRequest,
    JoinRoomRequest,
    RoomAuthResponse,
    RoomEvent,
    RoomLobbyView,
    SessionView,
    StartRoomRequest,
)
from src.services.room_service import GameRoom, RoomMember, room_service
from src.services.session_service import session_service

router = APIRouter(prefix="/api/rooms", tags=["rooms"])
RoomIdentity = Annotated[tuple[GameRoom, RoomMember], Depends(require_room_member)]


@router.post("", response_model=RoomAuthResponse, status_code=201)
async def create_room(request: CreateRoomRequest) -> RoomAuthResponse:
    """创建房间、选择角色并签发房主令牌。"""
    room, member = await room_service.create_room(
        display_name=request.display_name,
        character_id=request.character_id,
        campaign_id=request.campaign_id,
    )
    return RoomAuthResponse(
        access_token=member.access_token,
        member=room_service.member_view(member),
        room=room_service.lobby_view(room),
    )


@router.get("/characters", response_model=list[CharacterOption])
async def list_characters() -> list[CharacterOption]:
    """读取创建房间时可选择的预设角色。"""
    return room_service.character_catalog()


@router.get("/{room_code}/lobby", response_model=RoomLobbyView)
async def get_lobby(room_code: str) -> RoomLobbyView:
    """读取大厅成员和可选角色。"""
    return room_service.lobby_view(room_service.require_room(room_code))


@router.post("/{room_code}/join", response_model=RoomAuthResponse)
async def join_room(room_code: str, request: JoinRoomRequest) -> RoomAuthResponse:
    """通过房间码和预设角色加入大厅。"""
    room, member = await room_service.join_room(
        room_code,
        display_name=request.display_name,
        character_id=request.character_id,
    )
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
    return RoomAuthResponse(
        access_token=member.access_token,
        member=room_service.member_view(member),
        room=lobby,
    )


@router.post("/{room_code}/start", response_model=SessionView)
async def start_room(request: StartRoomRequest, identity: RoomIdentity) -> SessionView:
    """由房主锁定阵容并启动真实 LLM 冒险。"""
    room, member = identity
    payload = await session_service.start(room, member, request.opening)
    await session_service.broadcast_session(room, payload)
    return session_service.session_view(room, member, payload)


@router.get("/{room_code}", response_model=SessionView)
async def get_room_session(identity: RoomIdentity) -> SessionView:
    """读取当前成员个性化会话视图，用于刷新恢复。"""
    room, member = identity
    payload = await session_service.current_payload(room)
    if payload is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="房间尚未开局")
    return session_service.session_view(room, member, payload)
