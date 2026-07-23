"""接口鉴权依赖。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException

from src.services.room_service import GameRoom, RoomMember, room_service


def bearer_token(authorization: str | None) -> str:
    """从 Authorization 读取匿名房间令牌。"""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="缺少房间访问令牌")
    return token.strip()


async def require_room_member(
    room_code: str,
    authorization: Annotated[str | None, Header()] = None,
) -> tuple[GameRoom, RoomMember]:
    """校验当前请求属于房间成员。"""
    return room_service.authenticate(room_code, bearer_token(authorization))
