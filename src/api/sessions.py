"""多人会话命令、行动与骰子路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from src.api.dependencies import require_room_member
from src.schemas.room import (
    AbilityIncreaseRequest,
    DiceRollRequest,
    DiceRollResult,
    InteractionRollResponse,
    RoomActionRequest,
    SendMessageRequest,
    SessionView,
)
from src.services.room_service import GameRoom, RoomMember, room_service
from src.services.session_service import session_service

router = APIRouter(prefix="/api/rooms", tags=["session"])
RoomIdentity = Annotated[tuple[GameRoom, RoomMember], Depends(require_room_member)]


@router.post("/{room_code}/messages", response_model=SessionView)
async def send_message(
    request: SendMessageRequest, identity: RoomIdentity
) -> SessionView:
    """提交玩家自然语言行动并广播推进结果。"""
    room, member = identity
    payload = await session_service.message(room, member, request.content)
    await session_service.broadcast_session(room, payload)
    return session_service.session_view(room, member, payload)


@router.post("/{room_code}/actions", response_model=SessionView)
async def submit_action(
    request: RoomActionRequest, identity: RoomIdentity
) -> SessionView:
    """提交当前中断允许的上下文行动。"""
    room, member = identity
    payload = await session_service.submit_action(
        room, member, request.model_dump(exclude_none=True)
    )
    await session_service.broadcast_session(room, payload)
    return session_service.session_view(room, member, payload)


@router.post("/{room_code}/level-ups", response_model=SessionView)
async def apply_level_up(
    request: AbilityIncreaseRequest, identity: RoomIdentity
) -> SessionView:
    """为当前玩家提交一轮属性提升并广播最新角色状态。"""
    room, member = identity
    payload = await session_service.apply_level_up(room, member, request.increases)
    await session_service.broadcast_session(room, payload)
    return session_service.session_view(room, member, payload)


@router.post("/{room_code}/interactions/roll", response_model=InteractionRollResponse)
async def roll_interaction(identity: RoomIdentity) -> InteractionRollResponse:
    """为当前玩家中断生成服务器可信骰值。"""
    room, member = identity
    payload, roll = await session_service.roll_interaction(room, member)
    await session_service.broadcast_session(room, payload)
    return InteractionRollResponse(
        roll=roll,
        session=session_service.session_view(room, member, payload),
    )


@router.post("/{room_code}/dice/roll", response_model=DiceRollResult)
async def free_roll(request: DiceRollRequest, identity: RoomIdentity) -> DiceRollResult:
    """自由投掷一颗资源库支持的多面骰并广播结果。"""
    room, member = identity
    roll = session_service.free_roll(room, member, request.dice_type)
    await room_service.bump_revision(room)
    await session_service.broadcast_roll(room, roll)
    return roll
