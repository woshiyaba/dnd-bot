"""FastAPI 应用装配入口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.invoke import router as invoke_router
from src.api.rooms import router as rooms_router
from src.api.sessions import router as sessions_router
from src.api.stories import router as stories_router
from src.api.websocket import router as websocket_router
from src.common.utils.llm_util import initialize_model_registry
from src.common.utils.log_util import ensure_logging_config
from src.services.story_service import story_service

ensure_logging_config()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """服务接收请求前完成模型目录校验与客户端初始化。"""
    initialize_model_registry()
    await story_service.start()
    try:
        yield
    finally:
        await story_service.stop()


app = FastAPI(
    title="DND BOT",
    description="一个支持匿名多人房间、可中断和可恢复的 D&D 跑团后端",
    version="0.2.0",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(invoke_router)
app.include_router(rooms_router)
app.include_router(sessions_router)
app.include_router(stories_router)
app.include_router(websocket_router)

# 构建后可由同一个服务提供网页、API 和 WebSocket，分享链接无需另配前端服务。
_PC_BUILD_DIR = Path(__file__).resolve().parents[1] / "front" / "pc-dnd-bot" / "dist"
if _PC_BUILD_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_PC_BUILD_DIR, html=True), name="pc")


async def create_app() -> FastAPI:
    """返回已完成路由装配的 FastAPI 应用。"""
    return app
