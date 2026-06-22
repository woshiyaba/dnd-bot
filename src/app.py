"""
FastAPI 应用：封装 LangGraph 图的调用，提供 RESTful 接口
"""

import logging
import time

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.common.utils.log_util import ensure_logging_config, get_elapsed_ms

from src.common.utils.json_parser import extract_json_object
from src.graph import invoke as graph_invoke
from src.common.ws.ws_manager import manager as ws_manager

ensure_logging_config()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="DEEP AGENTS TEMPLATE",
    description="一个deepagents的模板",
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

async def create_app() -> FastAPI:
    """工厂函数：创建并返回 FastAPI 应用实例"""
    logger.info("[app.create_app] 初始化应用并预热图实例")
    return app