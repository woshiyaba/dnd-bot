"""FastAPI 应用装配入口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.invoke import router as invoke_router
from src.api.rooms import router as rooms_router
from src.api.sessions import router as sessions_router
from src.api.stories import router as stories_router
from src.api.websocket import router as websocket_router
from src.common.utils.llm_util import initialize_model_registry
from src.common.utils.log_util import ensure_logging_config

ensure_logging_config()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """服务接收请求前完成模型目录校验与客户端初始化。"""
    initialize_model_registry()
    yield


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


async def create_app() -> FastAPI:
    """返回已完成路由装配的 FastAPI 应用。"""
    return app
