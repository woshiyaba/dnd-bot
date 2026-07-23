"""多人房间对 SessionEngine 的应用服务封装。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from src.common.ws.ws_manager import manager as ws_manager
from src.combat.dice import parse_dice, roll_virtual_dice
from src.model.enums import InterruptType
from src.schemas.room import (
    CharacterView,
    DiceRollResult,
    PendingInteractionView,
    RecentResolutionView,
    RoomEvent,
    RoomView,
    SceneView,
    SessionView,
    TimelineEntry,
)
from src.services.room_service import (
    CHARACTER_TEMPLATES,
    GameRoom,
    RoomMember,
    room_service,
)
from src.session.engine import SessionEngine
from src.story.loader import get_registry

logger = logging.getLogger(__name__)

_D20_INTERRUPTS = {
    InterruptType.ROLL_INITIATIVE.value,
    InterruptType.ATTACK_ROLL.value,
    InterruptType.SAVING_THROW.value,
    InterruptType.ABILITY_CHECK.value,
}
_DICE_TYPES = {4: "d4", 6: "d6", 8: "d8", 10: "d10", 12: "d12", 20: "d20"}


class SessionService:
    """串行驱动房间会话，并生成稳定的玩家公开视图。"""

    def __init__(self) -> None:
        self._engine: SessionEngine | None = None
        self._canon_loaded = False
        self._room_locks: dict[str, asyncio.Lock] = {}

    async def start(
        self, room: GameRoom, member: RoomMember, opening: str
    ) -> dict[str, Any]:
        """房主启动多人会话并返回内部统一负载。"""
        if not member.is_host:
            raise HTTPException(status_code=403, detail="只有房主可以开始冒险")
        async with self._room_lock(room.room_code):
            await room_service.mark_started(room)
            payload = await self._get_engine().start_session_stream(
                room.room_code,
                room_service.scene_context(room),
                opening=opening,
                event_sink=self.stream_sink(room),
            )
            await room_service.bump_revision(room)
        await self._sync_room_status(room, payload)
        return payload

    async def message(
        self, room: GameRoom, member: RoomMember, content: str
    ) -> dict[str, Any]:
        """提交一个带真实发送者上下文的玩家行动。"""
        self._require_playing(room)
        async with self._room_lock(room.room_code):
            current = await self._require_payload(room.room_code)
            if current.get("status") != "awaiting_input":
                raise HTTPException(status_code=409, detail="当前会话不接受自由输入")
            payload = await self._get_engine().message_stream(
                room.room_code,
                content,
                user_id=member.user_id,
                actor_id=member.character_id,
                display_name=member.display_name,
                event_sink=self.stream_sink(room),
            )
            await room_service.bump_revision(room)
        await self._sync_room_status(room, payload)
        return payload

    async def submit_action(
        self, room: GameRoom, member: RoomMember, action: dict[str, Any]
    ) -> dict[str, Any]:
        """校验并提交当前角色的上下文行动。"""
        self._require_playing(room)
        async with self._room_lock(room.room_code):
            current = await self._require_payload(room.room_code)
            interrupt_request = self.require_pending_interrupt(current, member)
            if (
                interrupt_request.get("interrupt_type")
                != InterruptType.DECLARE_ACTION.value
            ):
                raise HTTPException(status_code=409, detail="当前交互不是行动声明")
            resume_value = self.validate_action_resume(
                interrupt_request.get("options") or {}, action
            )
            payload = await self._get_engine().submit_stream(
                room.room_code,
                resume_value,
                event_sink=self.stream_sink(room),
            )
            await room_service.bump_revision(room)
        await self._sync_room_status(room, payload)
        return payload

    async def roll_interaction(
        self, room: GameRoom, member: RoomMember
    ) -> tuple[dict[str, Any], DiceRollResult]:
        """为当前中断生成服务器骰值并恢复会话。"""
        self._require_playing(room)
        async with self._room_lock(room.room_code):
            current = await self._require_payload(room.room_code)
            interrupt_request = self.require_pending_interrupt(current, member)
            kind = interrupt_request.get("interrupt_type")
            if kind == InterruptType.DECLARE_ACTION.value:
                raise HTTPException(status_code=409, detail="当前交互需要选择行动")
            if kind not in _D20_INTERRUPTS | {InterruptType.DAMAGE_ROLL.value}:
                raise HTTPException(status_code=409, detail="当前交互不支持虚拟骰")
            expression = str(interrupt_request.get("required_dice") or "").strip()
            if not expression:
                raise HTTPException(status_code=409, detail="当前交互没有骰子表达式")
            extra = interrupt_request.get("extra") or {}
            try:
                if kind in _D20_INTERRUPTS and parse_dice(expression) != (1, 20, 0):
                    raise ValueError("d20 中断必须要求一颗无修正 d20")
                result = roll_virtual_dice(expression, crit=bool(extra.get("crit")))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            roll = self._roll_view(
                room,
                member,
                purpose="interaction",
                expression=expression,
                rolls=result.rolls,
                modifier=result.modifier,
                total=result.total,
            )
            resume_value = (
                {"result": result.total, "source": "virtual"}
                if kind == InterruptType.DAMAGE_ROLL.value
                else {"d20": result.total, "source": "virtual"}
            )
            payload = await self._get_engine().submit_stream(
                room.room_code,
                resume_value,
                event_sink=self.stream_sink(room),
            )
            await room_service.bump_revision(room)
        await self._sync_room_status(room, payload)
        return payload, roll

    def free_roll(
        self, room: GameRoom, member: RoomMember, dice_type: str
    ) -> DiceRollResult:
        """生成不推进会话的房间自由骰。"""
        result = roll_virtual_dice(dice_type)
        return self._roll_view(
            room,
            member,
            purpose="free",
            expression=dice_type,
            rolls=result.rolls,
            modifier=result.modifier,
            total=result.total,
        )

    async def current_payload(self, room: GameRoom) -> dict[str, Any] | None:
        """读取已开始房间的当前统一负载。"""
        if room.status == "lobby":
            return None
        return await self._get_engine().current_payload(room.room_code)

    def session_view(
        self,
        room: GameRoom,
        member: RoomMember,
        payload: dict[str, Any] | None,
    ) -> SessionView:
        """把内部引擎负载投影成面向单个玩家的稳定 DTO。"""
        state = (payload or {}).get("state") or {}
        safe_state = jsonable_encoder(state)
        interrupt = (payload or {}).get("interrupt")
        combat_view = ((interrupt or {}).get("extra") or {}).get("combat") or {}
        party_source = list((safe_state.get("party") or {}).values())
        combatants = combat_view.get("combatants") or []
        if combatants:
            party_source = [
                actor
                for actor in combatants
                if actor.get("faction") == "player"
                or actor.get("id") in {m.character_id for m in room.members.values()}
            ]
        enemies_source = (
            [
                actor
                for actor in combatants
                if actor.get("faction") not in {"player", None}
                and actor.get("id")
                not in {m.character_id for m in room.members.values()}
            ]
            if combatants
            else self._scene_enemies(safe_state.get("scene") or {})
        )
        scene_data = safe_state.get("scene") or {}
        pending = self._pending_view(room, member, interrupt)
        members_by_character = {
            item.character_id: item for item in room.members.values()
        }
        session_status = (payload or {}).get("status", "idle")
        return SessionView(
            room=RoomView(
                room_code=room.room_code,
                campaign_id=room.campaign_id,
                status=room.status,
                revision=room.revision,
                is_host=member.is_host,
                online_count=sum(m.is_online for m in room.members.values()),
                member_count=len(room.members),
            ),
            session_status=session_status,
            scene=SceneView(
                location=scene_data.get("location") or "未知地点",
                description=scene_data.get("description") or "",
                exits=list(scene_data.get("exits") or []),
                threat=scene_data.get("threat"),
                image="/scene-dungeon.jpg",
                round=combat_view.get("round"),
                phase="战斗阶段" if combat_view else "冒险阶段",
            ),
            party=[
                self._character_view(
                    actor,
                    member,
                    members_by_character.get(actor.get("id")),
                )
                for actor in party_source
            ],
            enemies=[
                self._character_view(actor, member, None) for actor in enemies_source
            ],
            timeline=self._timeline(
                [
                    *(safe_state.get("messages") or []),
                    *(combat_view.get("feed") or []),
                ],
                room=room,
            ),
            pending_interaction=pending,
            recent_resolution=RecentResolutionView(
                check=(payload or {}).get("last_check") or safe_state.get("last_check"),
                combat=(payload or {}).get("last_combat")
                or safe_state.get("last_combat"),
            ),
        )

    def stream_sink(self, room: GameRoom):
        """把 LangGraph custom 流转成房间统一事件。"""

        async def sink(event: dict[str, Any]) -> None:
            if event.get("type") == "debug_node" or event.get("status") == "debug":
                return
            status = event.get("status", "streaming")
            event_type = (
                "dm_stream_start"
                if status == "start"
                else "dm_stream_end" if status == "end" else "dm_stream"
            )
            payload = {
                "node": event.get("node", ""),
                "content": event.get("chunk", ""),
            }
            await ws_manager.broadcast_room(
                room.room_code,
                RoomEvent(
                    type=event_type,
                    room_code=room.room_code,
                    revision=room.revision,
                    payload=payload,
                ).model_dump(),
            )

        return sink

    async def broadcast_session(self, room: GameRoom, payload: dict[str, Any]) -> None:
        """向每个成员发送按身份裁剪后的最新会话视图。"""
        for member in room.members.values():
            view = self.session_view(room, member, payload)
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

    async def broadcast_roll(self, room: GameRoom, roll: DiceRollResult) -> None:
        """向全房间广播服务器骰结果。"""
        await ws_manager.broadcast_room(
            room.room_code,
            RoomEvent(
                type="dice_rolled",
                room_code=room.room_code,
                revision=room.revision,
                payload={"roll": roll.model_dump()},
            ).model_dump(),
        )

    @staticmethod
    def require_pending_interrupt(
        payload: dict[str, Any], member: RoomMember
    ) -> dict[str, Any]:
        """校验当前交互确实属于请求成员。"""
        interrupt_request = payload.get("interrupt")
        if payload.get("status") != "interrupted" or not isinstance(
            interrupt_request, dict
        ):
            raise HTTPException(status_code=409, detail="当前会话没有待处理交互")
        directed_user = (interrupt_request.get("directed_to") or {}).get("user_id")
        if directed_user and directed_user != member.user_id:
            raise HTTPException(status_code=403, detail="当前交互不属于该玩家")
        return interrupt_request

    @staticmethod
    def validate_action_resume(
        options: dict[str, Any], action: dict[str, Any]
    ) -> dict[str, Any]:
        """校验声明行动必须来自引擎给出的合法选项。"""
        action_type = action.get("action_type")
        if action_type == "pass" and options.get("pass"):
            return {"action_type": "pass"}
        if action_type == "move":
            zone = action.get("target_zone")
            allowed = {item.get("target_zone") for item in options.get("move", [])}
            if zone in allowed:
                return {"action_type": "move", "target_zone": zone}
        if action_type == "attack":
            attack_name = action.get("attack_name")
            target_id = action.get("target_id")
            for attack in options.get("attack", []):
                targets = {target.get("id") for target in attack.get("targets", [])}
                if attack.get("attack_name") == attack_name and target_id in targets:
                    return {
                        "action_type": "attack",
                        "attack_name": attack_name,
                        "target_id": target_id,
                    }
        if action_type in {"skill", "item"}:
            key = "skill_id" if action_type == "skill" else "item_id"
            value = action.get(key)
            allowed = {item.get(key) for item in options.get(action_type, [])}
            if value in allowed:
                normalized = {"action_type": action_type, key: value}
                if action.get("target_id"):
                    normalized["target_id"] = action["target_id"]
                return normalized
        if action_type == "special":
            special_action_id = action.get("special_action_id")
            for option in options.get("special", []):
                if option.get("special_action_id") == special_action_id:
                    return {
                        "action_type": "special",
                        "special_action_id": special_action_id,
                        "target_id": option.get("target_id"),
                    }
        if action_type == "natural_language" and options.get("natural_language"):
            description = str(action.get("description") or "").strip()
            if description:
                return {
                    "action_type": "natural_language",
                    "description": description,
                }
        raise HTTPException(status_code=422, detail="行动不在当前合法选项中")

    def _get_engine(self) -> SessionEngine:
        self._ensure_canon_loaded()
        if self._engine is None:
            self._engine = SessionEngine()
        return self._engine

    def _ensure_canon_loaded(self) -> None:
        if self._canon_loaded:
            return
        loaded = get_registry().load_all()
        self._canon_loaded = True
        logger.info("[canon] 注册表加载完成 | count=%d", len(loaded))

    def _room_lock(self, room_code: str) -> asyncio.Lock:
        return self._room_locks.setdefault(room_code, asyncio.Lock())

    async def _require_payload(self, room_code: str) -> dict[str, Any]:
        payload = await self._get_engine().current_payload(room_code)
        if payload is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return payload

    @staticmethod
    def _require_playing(room: GameRoom) -> None:
        if room.status != "playing":
            raise HTTPException(status_code=409, detail="房间尚未开局或已经结束")

    async def _sync_room_status(self, room: GameRoom, payload: dict[str, Any]) -> None:
        if payload.get("status") == "finished":
            await room_service.mark_finished(room)

    def _pending_view(
        self,
        room: GameRoom,
        member: RoomMember,
        interrupt: dict[str, Any] | None,
    ) -> PendingInteractionView | None:
        if not interrupt:
            return None
        directed = interrupt.get("directed_to") or {}
        directed_user_id = directed.get("user_id")
        directed_member = room_service.member_for_user(room, directed_user_id)
        is_yours = not directed_user_id or directed_user_id == member.user_id
        return PendingInteractionView(
            interrupt_type=str(interrupt.get("interrupt_type") or ""),
            prompt=str(interrupt.get("prompt") or "等待玩家操作"),
            required_dice=interrupt.get("required_dice"),
            bonus=int(interrupt.get("bonus") or 0),
            directed_to_user_id=directed_user_id,
            directed_to_character_id=directed.get("combatant_id"),
            directed_to_name=(
                directed_member.display_name if directed_member else None
            ),
            is_yours=is_yours,
            options=interrupt.get("options") if is_yours else None,
        )

    @staticmethod
    def _scene_enemies(scene: dict[str, Any]) -> list[dict[str, Any]]:
        enemies = []
        for actor in scene.get("actors") or []:
            if actor.get("disposition") != "hostile":
                continue
            card = dict(actor.get("card") or {})
            card["id"] = actor.get("actor_id") or card.get("id")
            card["name"] = actor.get("name") or card.get("name")
            enemies.append(card)
        return enemies

    @staticmethod
    def _timeline(
        messages: list[dict[str, Any]],
        *,
        room: GameRoom | None = None,
    ) -> list[TimelineEntry]:
        """把剧情消息和战斗内公开消息合并为统一时间线。"""
        timeline = []
        for index, message in enumerate(messages):
            raw_role = message.get("role")
            role = (
                "player"
                if raw_role in {"user", "player"}
                else ("dm" if raw_role == "dm" else "system")
            )
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            character_id = message.get("character_id")
            sender = (
                next(
                    (
                        member
                        for member in room.members.values()
                        if member.character_id == character_id
                    ),
                    None,
                )
                if room and character_id
                else None
            )
            timeline.append(
                TimelineEntry(
                    id=str(message.get("id") or f"message-{index}"),
                    role=role,
                    content=content,
                    sender_user_id=message.get("sender_user_id")
                    or (sender.user_id if sender else None),
                    sender_name=message.get("sender_name")
                    or (sender.display_name if sender else None),
                    character_id=character_id,
                )
            )
        return timeline

    @staticmethod
    def _character_view(
        actor: dict[str, Any],
        current_member: RoomMember,
        owner: RoomMember | None,
    ) -> CharacterView:
        template = CHARACTER_TEMPLATES.get(actor.get("id"), {})
        conditions = []
        for condition in actor.get("conditions") or []:
            if isinstance(condition, dict):
                value = condition.get("condition_type") or condition.get("name")
            else:
                value = condition
            conditions.append(str(value))
        life_state = actor.get("life_state")
        if isinstance(life_state, dict):
            life_state = life_state.get("value")
        return CharacterView(
            id=str(actor.get("id") or ""),
            name=str(actor.get("name") or "未知角色"),
            race=actor.get("race"),
            char_class=actor.get("char_class"),
            level=int(actor.get("level") or 1),
            current_hp=int(actor.get("current_hp") or 0),
            max_hp=max(int(actor.get("max_hp") or 1), 1),
            ac=int(actor.get("ac") or 10),
            life_state=str(life_state) if life_state else None,
            conditions=conditions,
            current_zone=actor.get("current_zone"),
            controller_user_id=owner.user_id if owner else None,
            display_name=owner.display_name if owner else None,
            is_self=bool(owner and owner.user_id == current_member.user_id),
            is_online=bool(owner and owner.is_online),
            color=str(template.get("color") or "#c9922a"),
        )

    @staticmethod
    def _roll_view(
        room: GameRoom,
        member: RoomMember,
        *,
        purpose: str,
        expression: str,
        rolls: list[int],
        modifier: int,
        total: int,
    ) -> DiceRollResult:
        _, faces, _ = parse_dice(expression)
        dice_type = _DICE_TYPES.get(faces)
        if dice_type is None:
            raise HTTPException(status_code=409, detail="前端没有对应的骰子资源")
        return DiceRollResult(
            roll_id=f"roll_{secrets.token_hex(8)}",
            room_code=room.room_code,
            purpose=purpose,
            expression=expression,
            dice_type=dice_type,
            rolls=rolls,
            modifier=modifier,
            total=total,
            user_id=member.user_id,
            display_name=member.display_name,
            character_id=member.character_id,
            created_at=datetime.now(UTC).isoformat(),
        )


session_service = SessionService()
