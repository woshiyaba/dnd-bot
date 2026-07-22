"""FastAPI 应用：封装 LangGraph 图的调用，提供 RESTful 接口。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.common.utils.log_util import ensure_logging_config, get_elapsed_ms
from src.common.ws.ws_manager import manager as ws_manager
from src.combat.dice import parse_dice, roll_virtual_dice
from src.model.enums import InterruptType
from src.graph import invoke as graph_invoke
from src.session.engine import SessionEngine
from src.story.loader import get_registry

ensure_logging_config()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="DND BOT",
    description="一个可中断、可恢复的 D&D 跑团后端",
    version="0.1.0",
)
# 允许跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_session_engine: SessionEngine | None = None
_canon_loaded = False
_session_locks: dict[str, asyncio.Lock] = {}


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket 端点：前端传入 user_id 建立长连接，后续 invoke 时实时推送数据"""
    await ws_manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, websocket)


class InvokeRequest(BaseModel):
    """调用请求模型"""

    user_input: str = Field(..., description="用户输入的消息")
    thread_id: str = Field(default="default", description="会话线程 ID")
    user_id: str = Field(default="用户ID", description="用户ID")


class InvokeResponse(BaseModel):
    """模板图调用响应模型。"""

    user_input: str = Field(..., description="用户输入的消息")
    thread_id: str = Field(default="default", description="会话线程 ID")
    user_id: str = Field(default="用户ID", description="用户ID")
    result: str = Field(..., description="返回结果")


@app.post("/invoke", response_model=InvokeResponse)
async def invoke_graph(request: InvokeRequest):
    """
    调用完整 LangGraph 流程

    执行流程：
    1. 并行执行 Analyze Agent 和 Strategy Agent
    2. SFTB Agent 生成策略蓝图
    3. Wording Agent 进行话术个性化转换
    4. Polishing Agent 进行语义润色
    """
    start_time = time.perf_counter()
    if request.user_id:
        await ws_manager.send_json(
            request.user_id,
            {
                "type": "flow_start",
                "thread_id": request.thread_id,
                "user_id": request.user_id,
            },
        )

    result = await graph_invoke(
        user_id=request.user_id,
    )
    response = InvokeResponse(
        user_input=result.get("user_input", ""),
        thread_id=result.get("thread_id", ""),
        user_id=result.get("user_id", ""),
        result=result.get("result", ""),
    )
    elapsed_ms = get_elapsed_ms(start_time)

    if request.user_id:
        await ws_manager.send_json(
            request.user_id,
            {
                "type": "flow_end",
                "status": "success",
                "thread_id": request.thread_id,
                "user_id": request.user_id,
            },
        )
    return response


class SessionStartRequest(BaseModel):
    """会话开局请求模型。"""

    room_id: str = Field(default="demo_room", description="房间 ID")
    user_id: str = Field(default="user_aria", description="玩家用户 ID")
    campaign_id: str = Field(default="whispers_bell_tower", description="剧情圣经 ID")
    dm_mode: str = Field(default="llm", description="DM 模式：固定为 llm")
    opening: str = Field(
        default="我推开破钟酒馆的门，走向村长。",
        description="开局玩家输入",
    )
    random_seed: int = Field(default=20260626, description="可复现随机种子")


class SessionMessageRequest(BaseModel):
    """玩家消息请求模型。"""

    user_id: str = Field(default="user_aria", description="玩家用户 ID")
    user_input: str = Field(..., description="玩家自然语言行动")


class SessionSubmitRequest(BaseModel):
    """中断恢复请求模型。"""

    user_id: str = Field(default="user_aria", description="玩家用户 ID")
    resume_value: dict[str, Any] = Field(..., description="恢复值，如 {'d20': 18}")


class SessionRollRequest(BaseModel):
    """服务端虚拟骰请求模型。"""

    user_id: str = Field(default="user_aria", description="玩家用户 ID")


@app.post("/session/start")
async def start_session(request: SessionStartRequest):
    """开启一局可玩的 D&D 冒险会话。"""
    start_time = time.perf_counter()
    engine = _get_session_engine()
    scene_context = _build_default_scene_context(request)
    async with _get_session_lock(request.room_id):
        payload = await engine.start_session_stream(
            request.room_id,
            scene_context,
            opening=request.opening,
            event_sink=_build_session_stream_sink(request.user_id, request.room_id),
        )
    safe_payload = _public_payload(payload)
    await _push_session_event(request.user_id, "session_start", safe_payload)
    logger.info(
        "[session.start] 开局完成 | room_id=%s | status=%s | elapsed_ms=%.2f",
        request.room_id,
        safe_payload.get("status"),
        get_elapsed_ms(start_time),
    )
    return JSONResponse(safe_payload)


@app.post("/session/{room_id}/message")
async def send_session_message(room_id: str, request: SessionMessageRequest):
    """提交玩家自然语言行动，推进一个 DM 回合。"""
    start_time = time.perf_counter()
    async with _get_session_lock(room_id):
        current = await _require_session_payload(room_id)
        _require_session_user(current, request.user_id)
        if current.get("status") != "awaiting_input":
            raise HTTPException(status_code=409, detail="当前会话不接受自由输入")
        payload = await _get_session_engine().message_stream(
            room_id,
            request.user_input,
            event_sink=_build_session_stream_sink(request.user_id, room_id),
        )
    safe_payload = _public_payload(payload)
    await _push_session_event(request.user_id, "session_update", safe_payload)
    logger.info(
        "[session.message] 回合完成 | room_id=%s | status=%s | elapsed_ms=%.2f",
        room_id,
        safe_payload.get("status"),
        get_elapsed_ms(start_time),
    )
    return JSONResponse(safe_payload)


@app.post("/session/{room_id}/submit")
async def submit_session_interrupt(room_id: str, request: SessionSubmitRequest):
    """提交掷骰或行动选择，恢复当前中断。"""
    start_time = time.perf_counter()
    async with _get_session_lock(room_id):
        current = await _require_session_payload(room_id)
        interrupt_request = _require_pending_interrupt(current, request.user_id)
        resume_value = _validate_manual_resume(interrupt_request, request.resume_value)
        payload = await _get_session_engine().submit_stream(
            room_id,
            resume_value,
            event_sink=_build_session_stream_sink(request.user_id, room_id),
        )
    safe_payload = _public_payload(payload)
    await _push_session_event(request.user_id, "session_update", safe_payload)
    logger.info(
        "[session.submit] 中断恢复完成 | room_id=%s | status=%s | elapsed_ms=%.2f",
        room_id,
        safe_payload.get("status"),
        get_elapsed_ms(start_time),
    )
    return JSONResponse(safe_payload)


@app.post("/session/{room_id}/roll")
async def roll_session_interrupt(room_id: str, request: SessionRollRequest):
    """由服务端生成当前中断要求的虚拟骰，并用可信结果恢复会话。"""
    start_time = time.perf_counter()
    async with _get_session_lock(room_id):
        current = await _require_session_payload(room_id)
        interrupt_request = _require_pending_interrupt(current, request.user_id)
        kind = interrupt_request.get("interrupt_type")
        if kind == InterruptType.DECLARE_ACTION.value:
            raise HTTPException(
                status_code=409, detail="当前中断需要选择行动，不能掷骰"
            )

        d20_kinds = {
            InterruptType.ROLL_INITIATIVE.value,
            InterruptType.ATTACK_ROLL.value,
            InterruptType.SAVING_THROW.value,
            InterruptType.ABILITY_CHECK.value,
        }
        if kind not in d20_kinds | {InterruptType.DAMAGE_ROLL.value}:
            raise HTTPException(status_code=409, detail="当前中断不支持虚拟骰")

        expression = str(interrupt_request.get("required_dice") or "").strip()
        if not expression:
            raise HTTPException(status_code=409, detail="当前中断没有可掷的骰子")
        extra = interrupt_request.get("extra") or {}
        try:
            if kind in d20_kinds and parse_dice(expression) != (1, 20, 0):
                raise ValueError("d20 中断必须要求一颗无修正 d20")
            roll = roll_virtual_dice(expression, crit=bool(extra.get("crit")))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        roll_event = {
            "room_id": room_id,
            "interrupt_type": kind,
            "expression": expression,
            "rolls": roll.rolls,
            "modifier": roll.modifier,
            "total": roll.total,
            "source": "virtual",
        }
        await _push_session_event(request.user_id, "roll_result", roll_event)

        if kind == InterruptType.DAMAGE_ROLL.value:
            resume_value = {"result": roll.total, "source": "virtual"}
        else:
            resume_value = {"d20": roll.total, "source": "virtual"}
        payload = await _get_session_engine().submit_stream(
            room_id,
            resume_value,
            event_sink=_build_session_stream_sink(request.user_id, room_id),
        )

    safe_payload = _public_payload(payload)
    safe_payload["roll_result"] = roll_event
    await _push_session_event(request.user_id, "session_update", safe_payload)
    logger.info(
        "[session.roll] 虚拟骰完成 | room_id=%s | expression=%s | total=%s | elapsed_ms=%.2f",
        room_id,
        expression,
        roll.total,
        get_elapsed_ms(start_time),
    )
    return JSONResponse(safe_payload)


@app.get("/session/{room_id}/state")
async def get_session_state(room_id: str):
    """读取某个房间的当前会话状态，用于刷新恢复。"""
    payload = await _get_session_engine().current_payload(room_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse(_public_payload(payload))


def _get_session_engine() -> SessionEngine:
    """获取进程级会话引擎，并确保 canon 注册表已加载。"""
    global _session_engine
    _ensure_canon_loaded()
    if _session_engine is None:
        _session_engine = SessionEngine()
    return _session_engine


def _get_session_lock(room_id: str) -> asyncio.Lock:
    """获取房间级异步锁，避免同一个 LangGraph 中断被并发恢复。"""
    return _session_locks.setdefault(room_id, asyncio.Lock())


async def _require_session_payload(room_id: str) -> dict:
    """读取统一会话负载；会话不存在时转为公开 HTTP 错误。"""
    payload = await _get_session_engine().current_payload(room_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return payload


def _require_session_user(payload: dict, user_id: str) -> None:
    """校验单人会话归属，防止其他用户推进房间。"""
    owner = (payload.get("state") or {}).get("user_id")
    if owner and owner != user_id:
        raise HTTPException(status_code=403, detail="当前用户无权操作该会话")


def _require_pending_interrupt(payload: dict, user_id: str) -> dict:
    """返回当前待处理中断，并校验请求玩家正是目标操作者。"""
    _require_session_user(payload, user_id)
    interrupt_request = payload.get("interrupt")
    if payload.get("status") != "interrupted" or not isinstance(
        interrupt_request, dict
    ):
        raise HTTPException(status_code=409, detail="当前会话没有待处理交互")
    directed_user = (interrupt_request.get("directed_to") or {}).get("user_id")
    if directed_user and directed_user != user_id:
        raise HTTPException(status_code=403, detail="当前交互不属于该用户")
    return interrupt_request


def _strict_int(value: Any, *, field_name: str) -> int:
    """按 HTTP 信任边界读取整数，拒绝布尔值和非整数字符串。"""
    if isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{field_name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} 必须是整数") from exc
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(status_code=422, detail=f"{field_name} 必须是整数")
    if isinstance(value, str) and value.strip() != str(parsed):
        raise HTTPException(status_code=422, detail=f"{field_name} 必须是整数")
    return parsed


def _validate_manual_resume(interrupt_request: dict, resume_value: dict) -> dict:
    """校验实体骰或行动选择，并只保留当前中断允许的公开字段。"""
    kind = interrupt_request.get("interrupt_type")
    d20_kinds = {
        InterruptType.ROLL_INITIATIVE.value,
        InterruptType.ATTACK_ROLL.value,
        InterruptType.SAVING_THROW.value,
        InterruptType.ABILITY_CHECK.value,
    }
    if kind in d20_kinds:
        d20 = _strict_int(resume_value.get("d20"), field_name="d20")
        if not 1 <= d20 <= 20:
            raise HTTPException(status_code=422, detail="d20 必须在 1 到 20 之间")
        normalized: dict[str, Any] = {"d20": d20, "source": "manual"}
        if "damage_result" in resume_value:
            damage = _strict_int(
                resume_value["damage_result"], field_name="damage_result"
            )
            if damage < 0:
                raise HTTPException(status_code=422, detail="damage_result 不能小于 0")
            normalized["damage_result"] = damage
        return normalized

    if kind == InterruptType.DAMAGE_ROLL.value:
        if "result" not in resume_value:
            return {"source": "virtual"}
        result = _strict_int(resume_value["result"], field_name="result")
        if result < 0:
            raise HTTPException(status_code=422, detail="result 不能小于 0")
        return {"result": result, "source": "manual"}

    if kind != InterruptType.DECLARE_ACTION.value:
        raise HTTPException(status_code=409, detail="不支持的中断类型")
    return _validate_action_resume(interrupt_request.get("options") or {}, resume_value)


def _validate_action_resume(options: dict, resume_value: dict) -> dict:
    """校验声明行动必须来自引擎给出的合法选项。"""
    action_type = resume_value.get("action_type")
    if action_type == "pass":
        return {"action_type": "pass"}
    if action_type == "move":
        zone = resume_value.get("target_zone")
        allowed = {item.get("target_zone") for item in options.get("move", [])}
        if zone in allowed:
            return {"action_type": "move", "target_zone": zone}
    if action_type == "attack":
        attack_name = resume_value.get("attack_name")
        target_id = resume_value.get("target_id")
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
        value = resume_value.get(key)
        allowed = {item.get(key) for item in options.get(action_type, [])}
        if value in allowed:
            normalized = {"action_type": action_type, key: value}
            if resume_value.get("target_id"):
                normalized["target_id"] = resume_value["target_id"]
            return normalized
    if action_type == "improvise" and options.get("improvise"):
        description = str(resume_value.get("description") or "").strip()
        if description:
            return {"action_type": "improvise", "description": description}
    raise HTTPException(status_code=422, detail="行动不在当前合法选项中")


def _ensure_canon_loaded() -> None:
    """加载 canon 目录到进程内注册表；重复调用保持幂等。"""
    global _canon_loaded
    if _canon_loaded:
        return
    loaded = get_registry().load_all()
    _canon_loaded = True
    logger.info("[canon] 注册表加载完成 | count=%d", len(loaded))


def _build_default_scene_context(request: SessionStartRequest) -> dict:
    """构造演示切片的默认场景上下文。"""
    return {
        "campaign_id": request.campaign_id,
        "dm_mode": "llm",
        "random_seed": request.random_seed,
        "user_id": request.user_id,
        "party": [
            {
                "type": "player",
                "controller": request.user_id,
                "card": {
                    "id": "pc_aria",
                    "name": "艾莉亚",
                    "strength": 16,
                    "dexterity": 14,
                    "constitution": 14,
                    "intelligence": 12,
                    "wisdom": 12,
                    "charisma": 13,
                    "current_hp": 30,
                    "max_hp": 30,
                    "ac": 16,
                    "level": 3,
                    "race": "人类",
                    "char_class": "战士",
                    "save_proficiencies": ["strength", "constitution"],
                    "attacks": [
                        {
                            "name": "长剑",
                            "attack_bonus": 6,
                            "damage_dice": "1d8+4",
                            "damage_type": "slashing",
                            "range": "melee",
                        }
                    ],
                    "inventory": [{"item_id": "item_healing_potion", "quantity": 1}],
                },
            }
        ],
    }


def _public_payload(payload: dict) -> dict:
    """把引擎负载转成可直接返回前端的 JSON 安全结构。"""
    public = dict(payload)
    public.pop("ending_beat_id", None)
    if isinstance(public.get("state"), dict):
        public["state"] = _strip_private_state(public["state"])
    return _json_safe(public)


def _strip_private_state(state: dict) -> dict:
    """把内部 DMState 收窄为玩家可见状态，避免泄漏裁定工作区。"""
    public_keys = {
        "user_id",
        "room_id",
        "messages",
        "scene",
        "party",
        "campaign_id",
        "story_status",
        "last_check",
        "last_combat",
        "campaign_log",
    }
    public = {key: value for key, value in state.items() if key in public_keys}

    scene = dict(public.get("scene") or {})
    for key in ("beat_id", "dm_mode", "random_seed", "loot_table", "encounter_id"):
        scene.pop(key, None)
    public["scene"] = scene

    story = state.get("story") or {}
    public["story"] = {
        "visited_count": len(story.get("visited_beats", []) or []),
        "clue_count": len(story.get("delivered_clues", []) or []),
    }
    public["campaign_log"] = [
        event
        for event in (public.get("campaign_log") or [])
        if event.get("event")
        in {"narration", "ability_check", "combat", "enter_beat", "story_end"}
    ]
    return public


def _json_safe(value: Any) -> Any:
    """把 dataclass、枚举等领域对象编码成 JSON 可序列化对象。"""
    return jsonable_encoder(value)


async def _push_session_event(user_id: str, event_type: str, payload: dict) -> None:
    """通过 WebSocket 推送会话事件；无连接时静默跳过。"""
    if not user_id:
        return
    await ws_manager.send_json(
        user_id,
        {
            "type": event_type,
            "payload": payload,
        },
    )


def _build_session_stream_sink(user_id: str, room_id: str):
    """构造会话流式事件转发器，把 LangGraph custom 事件映射为前端协议。"""

    async def sink(event: dict[str, Any]) -> None:
        if not user_id:
            return
        if event.get("type") == "debug_node":
            await ws_manager.send_json(user_id, event)
            return
        if event.get("status") == "debug":
            debug_event = event.get("debug")
            if isinstance(debug_event, dict):
                await ws_manager.send_json(user_id, debug_event)
            return
        status = event.get("status", "streaming")
        node = event.get("node", "")
        if status == "start":
            await ws_manager.send_json(
                user_id,
                {
                    "type": "node_start",
                    "room_id": room_id,
                    "node": node,
                },
            )
        elif status == "end":
            await ws_manager.send_json(
                user_id,
                {
                    "type": "node_end",
                    "room_id": room_id,
                    "node": node,
                },
            )
        else:
            await ws_manager.send_json(
                user_id,
                {
                    "type": "stream",
                    "room_id": room_id,
                    "node": node,
                    "content": event.get("chunk", ""),
                },
            )

    return sink


async def create_app() -> FastAPI:
    """工厂函数：创建并返回 FastAPI 应用实例"""
    _ensure_canon_loaded()
    logger.info("[app.create_app] 初始化应用并加载 canon 注册表")
    return app
