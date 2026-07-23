"""多人房间、会话视图与骰子接口模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

RoomStatus = Literal["lobby", "playing", "finished"]
SessionStatus = Literal["idle", "awaiting_input", "interrupted", "finished"]
DiceType = Literal["d4", "d6", "d8", "d10", "d12", "d20"]


class CharacterOption(BaseModel):
    """大厅可选择的预设角色摘要。"""

    id: str
    name: str
    race: str
    char_class: str
    level: int
    max_hp: int
    ac: int
    initiative: int
    speed: str = "30ft"
    color: str
    available: bool = True


class MemberView(BaseModel):
    """房间成员公开信息。"""

    user_id: str
    display_name: str
    character_id: str
    is_host: bool
    is_online: bool


class RoomLobbyView(BaseModel):
    """未开局房间的公开大厅视图。"""

    room_code: str
    campaign_id: str
    status: RoomStatus
    revision: int
    max_players: int
    members: list[MemberView]
    characters: list[CharacterOption]


class CreateRoomRequest(BaseModel):
    """创建匿名多人房间。"""

    display_name: str = Field(min_length=1, max_length=24)
    character_id: str = Field(min_length=1, max_length=64)
    campaign_id: str = Field(default="whispers_bell_tower", min_length=1)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        """去除昵称首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("昵称不能为空")
        return normalized


class JoinRoomRequest(BaseModel):
    """通过房间码加入房间。"""

    display_name: str = Field(min_length=1, max_length=24)
    character_id: str = Field(min_length=1, max_length=64)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        """去除昵称首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("昵称不能为空")
        return normalized


class RoomAuthResponse(BaseModel):
    """创建或加入房间后的匿名身份凭据。"""

    access_token: str
    member: MemberView
    room: RoomLobbyView


class StartRoomRequest(BaseModel):
    """房主启动冒险时的首句。"""

    opening: str = Field(
        default="我们推开破钟酒馆的门，走向等候已久的村长。",
        min_length=1,
        max_length=500,
    )


class SendMessageRequest(BaseModel):
    """玩家提交给 DM 的自然语言行动。"""

    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        """去除行动文本首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("行动内容不能为空")
        return normalized


class RoomActionRequest(BaseModel):
    """声明战斗行动；服务端仍按当前中断白名单校验。"""

    action_type: Literal["attack", "move", "skill", "item", "improvise", "pass"]
    attack_name: str | None = None
    target_id: str | None = None
    target_zone: str | None = None
    skill_id: str | None = None
    item_id: str | None = None
    description: str | None = Field(default=None, max_length=500)


class DiceRollRequest(BaseModel):
    """自由骰请求。"""

    dice_type: DiceType


class DiceRollResult(BaseModel):
    """一次服务器可信掷骰的公开结果。"""

    roll_id: str
    room_code: str
    purpose: Literal["free", "interaction"]
    expression: str
    dice_type: DiceType
    rolls: list[int]
    modifier: int
    total: int
    user_id: str
    display_name: str
    character_id: str
    created_at: str


class TimelineEntry(BaseModel):
    """玩家可见时间线消息。"""

    id: str
    role: Literal["dm", "player", "system"]
    content: str
    sender_user_id: str | None = None
    sender_name: str | None = None
    character_id: str | None = None


class CharacterView(BaseModel):
    """游戏内角色公开状态。"""

    id: str
    name: str
    race: str | None = None
    char_class: str | None = None
    level: int = 1
    current_hp: int
    max_hp: int
    ac: int
    life_state: str | None = None
    conditions: list[str] = Field(default_factory=list)
    current_zone: str | None = None
    controller_user_id: str | None = None
    display_name: str | None = None
    is_self: bool = False
    is_online: bool = False
    color: str = "#c9922a"


class SceneView(BaseModel):
    """当前场景公开投影。"""

    location: str = "未知地点"
    description: str = ""
    exits: list[str] = Field(default_factory=list)
    threat: str | None = None
    image: str | None = None
    round: int | None = None
    phase: str | None = None


class PendingInteractionView(BaseModel):
    """面向当前访问者裁剪后的待处理交互。"""

    interrupt_type: str
    prompt: str
    required_dice: str | None = None
    bonus: int = 0
    directed_to_user_id: str | None = None
    directed_to_character_id: str | None = None
    directed_to_name: str | None = None
    is_yours: bool = False
    options: dict[str, Any] | None = None


class RecentResolutionView(BaseModel):
    """最近一次检定或战斗结算。"""

    check: dict[str, Any] | None = None
    combat: dict[str, Any] | None = None


class RoomView(BaseModel):
    """游戏页面顶部所需的房间信息。"""

    room_code: str
    campaign_id: str
    status: RoomStatus
    revision: int
    is_host: bool
    online_count: int
    member_count: int


class SessionView(BaseModel):
    """稳定、脱敏、面向单个房间成员的游戏视图。"""

    room: RoomView
    session_status: SessionStatus
    scene: SceneView
    party: list[CharacterView]
    enemies: list[CharacterView]
    timeline: list[TimelineEntry]
    pending_interaction: PendingInteractionView | None = None
    recent_resolution: RecentResolutionView = Field(
        default_factory=RecentResolutionView
    )


class InteractionRollResponse(BaseModel):
    """中断骰结果与推进后的会话视图。"""

    roll: DiceRollResult
    session: SessionView


class RoomEvent(BaseModel):
    """房间 WebSocket 的统一事件外壳。"""

    type: str
    room_code: str
    revision: int
    payload: dict[str, Any] = Field(default_factory=dict)
