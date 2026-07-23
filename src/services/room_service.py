"""匿名多人房间与预设角色管理。"""

from __future__ import annotations

import asyncio
import secrets
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from src.schemas.room import CharacterOption, MemberView, RoomLobbyView

_ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ROOM_CODE_LENGTH = 6
_MAX_PLAYERS = 6


def _attack(
    name: str,
    attack_bonus: int,
    damage_dice: str,
    damage_type: str,
    *,
    attack_range: str = "melee",
) -> dict[str, Any]:
    """构造预设角色的攻击卡。"""
    return {
        "name": name,
        "attack_bonus": attack_bonus,
        "damage_dice": damage_dice,
        "damage_type": damage_type,
        "range": attack_range,
    }


_CHARACTER_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "pc_aldous",
        "name": "艾伦",
        "race": "人类",
        "char_class": "战士",
        "level": 5,
        "current_hp": 52,
        "max_hp": 52,
        "ac": 18,
        "initiative_bonus": 2,
        "speed": "30ft",
        "color": "#c9922a",
        "strength": 18,
        "dexterity": 14,
        "constitution": 16,
        "intelligence": 10,
        "wisdom": 12,
        "charisma": 11,
        "save_proficiencies": ["strength", "constitution"],
        "attacks": [_attack("长剑", 7, "1d8+4", "slashing")],
        "inventory": [{"item_id": "item_healing_potion", "quantity": 1}],
    },
    {
        "id": "pc_lyra",
        "name": "莉拉",
        "race": "高等精灵",
        "char_class": "法师",
        "level": 5,
        "current_hp": 35,
        "max_hp": 35,
        "ac": 12,
        "initiative_bonus": 2,
        "speed": "30ft",
        "color": "#4a90d9",
        "strength": 8,
        "dexterity": 14,
        "constitution": 14,
        "intelligence": 18,
        "wisdom": 12,
        "charisma": 10,
        "save_proficiencies": ["intelligence", "wisdom"],
        "attacks": [_attack("火焰箭", 7, "2d10", "fire", attack_range="ranged")],
    },
    {
        "id": "pc_kate",
        "name": "卡特",
        "race": "半身人",
        "char_class": "盗贼",
        "level": 5,
        "current_hp": 38,
        "max_hp": 38,
        "ac": 15,
        "initiative_bonus": 4,
        "speed": "25ft",
        "color": "#7ab648",
        "strength": 10,
        "dexterity": 18,
        "constitution": 14,
        "intelligence": 13,
        "wisdom": 12,
        "charisma": 14,
        "save_proficiencies": ["dexterity", "intelligence"],
        "attacks": [_attack("短剑", 7, "1d6+4", "piercing")],
    },
    {
        "id": "pc_serra",
        "name": "塞拉",
        "race": "矮人",
        "char_class": "牧师",
        "level": 5,
        "current_hp": 44,
        "max_hp": 44,
        "ac": 16,
        "initiative_bonus": 1,
        "speed": "25ft",
        "color": "#c94a4a",
        "strength": 14,
        "dexterity": 12,
        "constitution": 16,
        "intelligence": 10,
        "wisdom": 18,
        "charisma": 13,
        "save_proficiencies": ["wisdom", "charisma"],
        "attacks": [_attack("战锤", 5, "1d8+2", "bludgeoning")],
    },
    {
        "id": "pc_rex",
        "name": "雷克",
        "race": "木精灵",
        "char_class": "游侠",
        "level": 5,
        "current_hp": 40,
        "max_hp": 40,
        "ac": 14,
        "initiative_bonus": 4,
        "speed": "35ft",
        "color": "#9b6ade",
        "strength": 12,
        "dexterity": 18,
        "constitution": 14,
        "intelligence": 11,
        "wisdom": 16,
        "charisma": 10,
        "save_proficiencies": ["strength", "dexterity"],
        "attacks": [_attack("长弓", 7, "1d8+4", "piercing", attack_range="ranged")],
    },
    {
        "id": "pc_vera",
        "name": "维拉",
        "race": "提夫林",
        "char_class": "术士",
        "level": 5,
        "current_hp": 36,
        "max_hp": 36,
        "ac": 13,
        "initiative_bonus": 3,
        "speed": "30ft",
        "color": "#4ab8b8",
        "strength": 8,
        "dexterity": 16,
        "constitution": 14,
        "intelligence": 12,
        "wisdom": 10,
        "charisma": 18,
        "save_proficiencies": ["constitution", "charisma"],
        "attacks": [_attack("魔能爆", 7, "1d10+4", "force", attack_range="ranged")],
    },
)

CHARACTER_TEMPLATES = {entry["id"]: entry for entry in _CHARACTER_TEMPLATES}


@dataclass(slots=True)
class RoomMember:
    """房间内的匿名玩家身份。"""

    user_id: str
    display_name: str
    character_id: str
    access_token: str
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
        """查找占用指定角色的成员。"""
        return next(
            (
                member
                for member in self.members.values()
                if member.character_id == character_id
            ),
            None,
        )


class RoomService:
    """管理匿名房间、成员令牌和预设角色占用。"""

    def __init__(self) -> None:
        self._rooms: dict[str, GameRoom] = {}
        self._registry_lock = asyncio.Lock()

    async def create_room(
        self, *, display_name: str, character_id: str, campaign_id: str
    ) -> tuple[GameRoom, RoomMember]:
        """创建房间并把创建者登记为房主。"""
        self._require_character(character_id)
        async with self._registry_lock:
            room_code = self._new_room_code()
            room = GameRoom(room_code=room_code, campaign_id=campaign_id)
            member = self._new_member(
                display_name=display_name,
                character_id=character_id,
                is_host=True,
            )
            room.members[member.user_id] = member
            self._rooms[room_code] = room
            return room, member

    async def join_room(
        self, room_code: str, *, display_name: str, character_id: str
    ) -> tuple[GameRoom, RoomMember]:
        """在开局前选择一个空闲角色加入房间。"""
        self._require_character(character_id)
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
            if room.member_by_character(character_id):
                raise HTTPException(status_code=409, detail="该角色已被其他玩家选择")
            member = self._new_member(
                display_name=display_name,
                character_id=character_id,
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
        occupied = {member.character_id for member in room.members.values()}
        characters = [
            self._character_option(template, template["id"] not in occupied)
            for template in _CHARACTER_TEMPLATES
        ]
        return RoomLobbyView(
            room_code=room.room_code,
            campaign_id=room.campaign_id,
            status=room.status,
            revision=room.revision,
            max_players=_MAX_PLAYERS,
            members=[self.member_view(member) for member in room.members.values()],
            characters=characters,
        )

    def character_catalog(self) -> list[CharacterOption]:
        """返回创建房间页使用的完整预设角色目录。"""
        return [
            self._character_option(template, True) for template in _CHARACTER_TEMPLATES
        ]

    def member_view(self, member: RoomMember) -> MemberView:
        """生成成员公开视图。"""
        return MemberView(
            user_id=member.user_id,
            display_name=member.display_name,
            character_id=member.character_id,
            is_host=member.is_host,
            is_online=member.is_online,
        )

    def scene_context(self, room: GameRoom) -> dict[str, Any]:
        """把房间成员选择转换为 SessionEngine 的多人初始上下文。"""
        host = next(member for member in room.members.values() if member.is_host)
        party = []
        for member in room.members.values():
            template = CHARACTER_TEMPLATES[member.character_id]
            card = {
                key: value
                for key, value in template.items()
                if key not in {"speed", "color"}
            }
            party.append(
                {
                    "type": "player",
                    "controller": member.user_id,
                    "card": card,
                }
            )
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

    def member_for_user(self, room: GameRoom, user_id: str | None) -> RoomMember | None:
        """读取房间内指定用户。"""
        return room.members.get(user_id or "")

    def character_template(self, character_id: str) -> dict[str, Any]:
        """读取预设角色完整卡面。"""
        return CHARACTER_TEMPLATES[self._require_character(character_id)]

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
        *, display_name: str, character_id: str, is_host: bool
    ) -> RoomMember:
        return RoomMember(
            user_id=f"user_{secrets.token_hex(8)}",
            display_name=display_name,
            character_id=character_id,
            access_token=secrets.token_urlsafe(32),
            is_host=is_host,
        )

    @staticmethod
    def _require_character(character_id: str) -> str:
        if character_id not in CHARACTER_TEMPLATES:
            raise HTTPException(status_code=422, detail="未知的预设角色")
        return character_id

    @staticmethod
    def _character_option(template: dict[str, Any], available: bool) -> CharacterOption:
        return CharacterOption(
            id=template["id"],
            name=template["name"],
            race=template["race"],
            char_class=template["char_class"],
            level=template["level"],
            max_hp=template["max_hp"],
            ac=template["ac"],
            initiative=template["initiative_bonus"],
            speed=template["speed"],
            color=template["color"],
            available=available,
        )


room_service = RoomService()
