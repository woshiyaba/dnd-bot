"""FastAPI 应用：封装 LangGraph 图的调用，提供 RESTful 接口。"""

from __future__ import annotations

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
    campaign_id: str = Field(
        default="whispers_bell_tower", description="剧情圣经 ID"
    )
    dm_mode: str = Field(default="heuristic", description="DM 模式：heuristic 或 llm")
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


@app.post("/session/start")
async def start_session(request: SessionStartRequest):
    """开启一局可玩的 D&D 冒险会话。"""
    start_time = time.perf_counter()
    engine = _get_session_engine()
    scene_context = _build_default_scene_context(request)
    payload = await engine.start_session(
        request.room_id,
        scene_context,
        opening=request.opening,
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
    payload = await _get_session_engine().message(room_id, request.user_input)
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
    payload = await _get_session_engine().submit(room_id, request.resume_value)
    safe_payload = _public_payload(payload)
    await _push_session_event(request.user_id, "session_update", safe_payload)
    logger.info(
        "[session.submit] 中断恢复完成 | room_id=%s | status=%s | elapsed_ms=%.2f",
        room_id,
        safe_payload.get("status"),
        get_elapsed_ms(start_time),
    )
    return JSONResponse(safe_payload)


@app.get("/session/{room_id}/state")
async def get_session_state(room_id: str):
    """读取某个房间的当前会话状态，用于刷新恢复。"""
    state = await _get_session_engine().current_state(room_id)
    if state is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse(_json_safe(_strip_private_state(state)))


def _get_session_engine() -> SessionEngine:
    """获取进程级会话引擎，并确保 canon 注册表已加载。"""
    global _session_engine
    _ensure_canon_loaded()
    if _session_engine is None:
        _session_engine = SessionEngine()
    return _session_engine


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
        "dm_mode": request.dm_mode,
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
                    "inventory": [
                        {"item_id": "item_healing_potion", "quantity": 1}
                    ],
                },
            }
        ],
    }


def _public_payload(payload: dict) -> dict:
    """把引擎负载转成可直接返回前端的 JSON 安全结构。"""
    public = dict(payload)
    if isinstance(public.get("state"), dict):
        public["state"] = _strip_private_state(public["state"])
    return _json_safe(public)


def _strip_private_state(state: dict) -> dict:
    """移除 LangGraph 内部中断对象，避免响应序列化泄漏运行时对象。"""
    public = dict(state)
    public.pop("__interrupt__", None)
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


async def create_app() -> FastAPI:
    """工厂函数：创建并返回 FastAPI 应用实例"""
    _ensure_canon_loaded()
    logger.info("[app.create_app] 初始化应用并加载 canon 注册表")
    return app
