"""FastAPI 应用装配入口。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.invoke import router as invoke_router
from src.api.rooms import router as rooms_router
from src.api.sessions import router as sessions_router
from src.api.websocket import router as websocket_router
from src.common.utils.log_util import ensure_logging_config

ensure_logging_config()

app = FastAPI(
    title="DND BOT",
    description="一个支持匿名多人房间、可中断和可恢复的 D&D 跑团后端",
    version="0.2.0",
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
app.include_router(websocket_router)


async def create_app() -> FastAPI:
    """返回已完成路由装配的 FastAPI 应用。"""
    return app
