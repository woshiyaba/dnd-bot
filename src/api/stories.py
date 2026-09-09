"""故事广场、LLM 访谈与 Canon 发布路由。"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, HTTPException, Request

from src.schemas.story import (
    StoryDraftRequest,
    StoryDraftResponse,
    StoryGenerationTaskResponse,
    StoryInterviewRequest,
    StoryInterviewResponse,
    StoryPublishResponse,
    StorySummary,
)
from src.services.story_service import story_service
from src.story.generator import StoryGenerationError
from src.story.store import StoryQueueFull, StoryRateLimitExceeded

router = APIRouter(prefix="/api/stories", tags=["stories"])
_MAX_BODY_BYTES = 128 * 1024


def _requester_key(request: Request) -> str:
    """只信任直连地址，不读取可伪造的转发请求头。"""
    host = request.client.host if request.client is not None else "unknown"
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


async def _require_body_limit(request: Request) -> None:
    """限制公开故事写接口的原始请求体。"""
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > _MAX_BODY_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail={
                "code": "story_input_too_large",
                "message": "故事请求体不能超过 128 KiB",
            },
        )
    if len(await request.body()) > _MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "story_input_too_large",
                "message": "故事请求体不能超过 128 KiB",
            },
        )


def _consume_limit(
    request: Request,
    bucket: str,
    *,
    limit: int,
    window_seconds: int,
) -> str:
    requester_key = _requester_key(request)
    try:
        story_service.consume_public_rate_limit(
            requester_key,
            bucket,
            limit=limit,
            window_seconds=window_seconds,
        )
    except StoryRateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "story_rate_limited", "message": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    return requester_key


def _raise_admission_error(exc: StoryRateLimitExceeded | StoryQueueFull) -> None:
    if isinstance(exc, StoryQueueFull):
        raise HTTPException(
            status_code=503,
            detail={"code": "story_queue_full", "message": str(exc)},
            headers={"Retry-After": "60"},
        ) from exc
    raise HTTPException(
        status_code=429,
        detail={"code": "story_rate_limited", "message": str(exc)},
        headers={"Retry-After": str(exc.retry_after)},
    ) from exc


@router.get("", response_model=list[StorySummary])
async def list_stories() -> list[StorySummary]:
    """列出所有已发布且可被游戏引擎加载的剧本。"""
    return story_service.list_stories()


@router.post("/interview", response_model=StoryInterviewResponse)
async def interview_story(
    request: Request,
    payload: StoryInterviewRequest,
) -> StoryInterviewResponse:
    """让真实 LLM 故事策划继续一轮结构化访谈。"""
    await _require_body_limit(request)
    _consume_limit(request, "interview", limit=30, window_seconds=3600)
    try:
        return await story_service.interview(
            conversation=[item.model_dump() for item in payload.conversation],
            design_brief=payload.design_brief,
        )
    except StoryGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/drafts", response_model=StoryDraftResponse, status_code=201)
async def create_story_draft(
    request: Request,
    payload: StoryDraftRequest,
) -> StoryDraftResponse:
    """把玩家已确认设计稿编译为通过校验的限时 Canon 草稿。"""
    await _require_body_limit(request)
    requester_key = _requester_key(request)
    try:
        return await story_service.create_draft_via_task(
            payload.design_brief,
            requester_key=requester_key,
        )
    except (StoryRateLimitExceeded, StoryQueueFull) as exc:
        _raise_admission_error(exc)
    except StoryGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/drafts/{draft_id}/publish",
    response_model=StoryPublishResponse,
    status_code=201,
)
async def publish_story(request: Request, draft_id: str) -> StoryPublishResponse:
    """将限时草稿原子发布到 canon 目录并立即注册。"""
    await _require_body_limit(request)
    _consume_limit(request, "publish", limit=20, window_seconds=60)
    return StoryPublishResponse(story=await story_service.publish(draft_id))


@router.post(
    "/generation-tasks",
    response_model=StoryGenerationTaskResponse,
    status_code=202,
)
async def create_story_generation_task(
    request: Request,
    payload: StoryDraftRequest,
) -> StoryGenerationTaskResponse:
    """提交可恢复的分阶段故事生成任务。"""
    await _require_body_limit(request)
    try:
        return await story_service.create_generation_task(
            payload.design_brief,
            requester_key=_requester_key(request),
        )
    except (StoryRateLimitExceeded, StoryQueueFull) as exc:
        _raise_admission_error(exc)


@router.get(
    "/generation-tasks/{task_id}",
    response_model=StoryGenerationTaskResponse,
)
async def get_story_generation_task(
    request: Request,
    task_id: str,
) -> StoryGenerationTaskResponse:
    """返回公开进度、脱敏错误以及完成后的限时草稿。"""
    _consume_limit(request, "task_status", limit=60, window_seconds=60)
    return story_service.get_generation_task(task_id)


@router.post(
    "/generation-tasks/{task_id}/retry",
    response_model=StoryGenerationTaskResponse,
    status_code=202,
)
async def retry_story_generation_task(
    request: Request, task_id: str
) -> StoryGenerationTaskResponse:
    """在累计预算限制内续跑一次失败任务。"""
    _consume_limit(request, "task_retry", limit=5, window_seconds=3600)
    try:
        return await story_service.retry_generation_task(
            task_id, requester_key=_requester_key(request)
        )
    except (StoryRateLimitExceeded, StoryQueueFull) as exc:
        _raise_admission_error(exc)


@router.delete(
    "/generation-tasks/{task_id}",
    response_model=StoryGenerationTaskResponse,
)
async def cancel_story_generation_task(
    request: Request,
    task_id: str,
) -> StoryGenerationTaskResponse:
    """请求在最近安全阶段边界取消生成。"""
    _consume_limit(request, "task_cancel", limit=20, window_seconds=60)
    return story_service.cancel_generation_task(task_id)
