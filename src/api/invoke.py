"""通用 deep-agent 调用路由。"""

from __future__ import annotations

import time

from fastapi import APIRouter

from src.common.utils.log_util import get_elapsed_ms
from src.common.ws.ws_manager import manager as ws_manager
from src.graph import invoke as graph_invoke
from src.schemas.invoke import InvokeRequest, InvokeResponse

router = APIRouter(tags=["agent"])


@router.post("/invoke", response_model=InvokeResponse)
async def invoke_graph(request: InvokeRequest) -> InvokeResponse:
    """调用当前 skills_find 图并通过旧用户通道发送生命周期事件。"""
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
    result = await graph_invoke(user_id=request.user_id)
    response = InvokeResponse(
        user_input=result.get("user_input", ""),
        thread_id=result.get("thread_id", ""),
        user_id=result.get("user_id", ""),
        result=result.get("result", ""),
    )
    if request.user_id:
        await ws_manager.send_json(
            request.user_id,
            {
                "type": "flow_end",
                "status": "success",
                "thread_id": request.thread_id,
                "user_id": request.user_id,
                "elapsed_ms": get_elapsed_ms(start_time),
            },
        )
    return response
