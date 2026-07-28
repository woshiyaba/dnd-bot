"""匿名多人房间与玩家自建角色管理。"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from src.character.creation import build_character_card, character_creation_catalog
from src.schemas.room import CharacterDraft, CharacterSummary, MemberView, RoomLobbyView

_ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ROOM_CODE_LENGTH = 6
_MAX_PLAYERS = 6


@dataclass(slots=True)
class RoomMember:
    """房间内的匿名玩家身份及其当前冒险角色卡。"""

    user_id: str
    display_name: str
    character_id: str
    access_token: str
    character_card: dict[str, Any] = field(default_factory=dict)
    is_host: bool = False
    is_online: bool = False


@dataclass(slots=True)
class GameRoom:
    """一局多人冒险的房间元数据。"""

    room_code: str
    campaign_id: str
    status: str = "lobby"
    revision: int = 1
    members: dict[str, RoomMember] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def member_by_token(self, access_token: str) -> RoomMember | None:
        """按不可预测令牌查找房间成员。"""
        return next(
            (
                member
                for member in self.members.values()
                if secrets.compare_digest(member.access_token, access_token)
            ),
            None,
        )

    def member_by_character(self, character_id: str) -> RoomMember | None:
        """按服务端角色 ID 查找其控制者。"""
        return next(
            (
                member
                for member in self.members.values()
                if member.character_id == character_id
            ),
            None,
        )


class RoomService:
    """管理匿名房间、成员令牌和服务端权威角色卡。"""

    def __init__(self) -> None:
        self._rooms: dict[str, GameRoom] = {}
        self._registry_lock = asyncio.Lock()

    async def create_room(
        self, *, display_name: str, character: CharacterDraft, campaign_id: str
    ) -> tuple[GameRoom, RoomMember]:
        """创建房间并用角色草稿登记房主。"""
        async with self._registry_lock:
            room_code = self._new_room_code()
            room = GameRoom(room_code=room_code, campaign_id=campaign_id)
            member = self._new_member(
                display_name=display_name,
                character=character,
                is_host=True,
            )
            room.members[member.user_id] = member
            self._rooms[room_code] = room
            return room, member

    async def join_room(
        self, room_code: str, *, display_name: str, character: CharacterDraft
    ) -> tuple[GameRoom, RoomMember]:
        """在开局前创建自己的角色并加入房间。"""
        async with self._registry_lock:
            room = self.require_room(room_code)
            if room.status != "lobby":
                raise HTTPException(status_code=409, detail="房间已经开局")
            if len(room.members) >= _MAX_PLAYERS:
                raise HTTPException(status_code=409, detail="房间人数已满")
            normalized_name = display_name.casefold()
            if any(
                member.display_name.casefold() == normalized_name
                for member in room.members.values()
            ):
                raise HTTPException(status_code=409, detail="房间内昵称已被使用")
            member = self._new_member(
                display_name=display_name,
                character=character,
                is_host=False,
            )
            room.members[member.user_id] = member
            room.revision += 1
            return room, member

    async def mark_started(self, room: GameRoom) -> None:
        """把房间切换到游戏中并锁定成员列表。"""
        async with self._registry_lock:
            if room.status != "lobby":
                raise HTTPException(status_code=409, detail="房间已经开局")
            if not room.members:
                raise HTTPException(status_code=409, detail="房间内没有玩家")
            room.status = "playing"
            room.revision += 1

    async def mark_finished(self, room: GameRoom) -> None:
        """同步会话结局到房间状态。"""
        async with self._registry_lock:
            if room.status != "finished":
                room.status = "finished"
                room.revision += 1

    async def bump_revision(self, room: GameRoom) -> int:
        """命令完成后推进房间视图版本。"""
        async with self._registry_lock:
            room.revision += 1
            return room.revision

    async def set_online(
        self, room: GameRoom, member: RoomMember, is_online: bool
    ) -> None:
        """更新成员在线状态并推进大厅版本。"""
        async with self._registry_lock:
            if member.is_online == is_online:
                return
            member.is_online = is_online
            room.revision += 1

    def require_room(self, room_code: str) -> GameRoom:
        """读取房间，不存在时返回公开 404。"""
        normalized = room_code.strip().upper()
        room = self._rooms.get(normalized)
        if room is None:
            raise HTTPException(status_code=404, detail="房间不存在")
        return room

    def authenticate(
        self, room_code: str, access_token: str
    ) -> tuple[GameRoom, RoomMember]:
        """校验房间访问令牌并返回对应成员。"""
        room = self.require_room(room_code)
        member = room.member_by_token(access_token)
        if member is None:
            raise HTTPException(status_code=401, detail="房间身份无效或已过期")
        return room, member

    def lobby_view(self, room: GameRoom) -> RoomLobbyView:
        """生成不含访问令牌的大厅公开视图。"""
        return RoomLobbyView(
            room_code=room.room_code,
            campaign_id=room.campaign_id,
            status=room.status,
            revision=room.revision,
            max_players=_MAX_PLAYERS,
            members=[self.member_view(member) for member in room.members.values()],
        )

    @staticmethod
    def creation_catalog() -> dict[str, Any]:
        """返回角色创建目录。"""
        return character_creation_catalog()

    def member_view(self, member: RoomMember) -> MemberView:
        """生成包含角色摘要的成员公开视图。"""
        return MemberView(
            user_id=member.user_id,
            display_name=member.display_name,
            character_id=member.character_id,
            character=self._character_summary(member.character_card),
            is_host=member.is_host,
            is_online=member.is_online,
        )

    def scene_context(self, room: GameRoom) -> dict[str, Any]:
        """把房间成员角色卡转换为 SessionEngine 的多人初始上下文。"""
        host = next(member for member in room.members.values() if member.is_host)
        party = [
            {
                "type": "player",
                "controller": member.user_id,
                "card": dict(member.character_card),
            }
            for member in room.members.values()
        ]
        return {
            "campaign_id": room.campaign_id,
            "dm_mode": "llm",
            "random_seed": secrets.randbelow(2_000_000_000),
            "user_id": host.user_id,
            "active_user_id": host.user_id,
            "active_actor_id": host.character_id,
            "active_display_name": host.display_name,
            "party": party,
        }

    def sync_character_cards(self, room: GameRoom, party: dict[str, Any]) -> None:
        """把会话中的最新角色对象同步回大厅成员摘要。"""
        for member in room.members.values():
            actor = party.get(member.character_id)
            if actor is not None and hasattr(actor, "to_card"):
                member.character_card = {**member.character_card, **actor.to_card()}

    def member_for_user(self, room: GameRoom, user_id: str | None) -> RoomMember | None:
        """读取房间内指定用户。"""
        return room.members.get(user_id or "")

    def character_card(self, character_id: str) -> dict[str, Any]:
        """读取当前房间之外不可寻址的角色卡接口已被移除。"""
        for room in self._rooms.values():
            member = room.member_by_character(character_id)
            if member is not None:
                return member.character_card
        raise HTTPException(status_code=404, detail="角色不存在")

    def reset(self) -> None:
        """清空进程内房间；仅供无模型测试隔离。"""
        self._rooms.clear()

    def _new_room_code(self) -> str:
        while True:
            value = "".join(
                secrets.choice(_ROOM_ALPHABET) for _ in range(_ROOM_CODE_LENGTH)
            )
            if value not in self._rooms:
                return value

    @staticmethod
    def _new_member(
        *, display_name: str, character: CharacterDraft, is_host: bool
    ) -> RoomMember:
        user_id = f"user_{secrets.token_hex(8)}"
        character_id = f"pc_{secrets.token_hex(8)}"
        try:
            card = build_character_card(
                character_id=character_id,
                name=display_name,
                race_id=character.race_id,
                class_id=character.class_id,
                base_abilities=character.base_abilities,
                racial_bonus_choices=character.racial_bonus_choices,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RoomMember(
            user_id=user_id,
            display_name=display_name,
            character_id=character_id,
            character_card=card,
            access_token=secrets.token_urlsafe(32),
            is_host=is_host,
        )

    @staticmethod
    def _character_summary(card: dict[str, Any]) -> CharacterSummary:
        return CharacterSummary(
            id=str(card["id"]),
            name=str(card["name"]),
            race_id=str(card["race_id"]),
            race=str(card["race"]),
            class_id=str(card["class_id"]),
            char_class=str(card["char_class"]),
            level=int(card.get("level", 1)),
            max_hp=int(card["max_hp"]),
            ac=int(card["ac"]),
            initiative=int(card.get("initiative_bonus", 0)),
            speed=str(card.get("speed", "30ft")),
            color=str(card.get("color", "#c9922a")),
        )


room_service = RoomService()
