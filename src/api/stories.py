"""故事广场、LLM 访谈与 Canon 发布路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

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

router = APIRouter(prefix="/api/stories", tags=["stories"])


@router.get("", response_model=list[StorySummary])
async def list_stories() -> list[StorySummary]:
    """列出所有已发布且可被游戏引擎加载的剧本。"""
    return story_service.list_stories()


@router.post("/interview", response_model=StoryInterviewResponse)
async def interview_story(request: StoryInterviewRequest) -> StoryInterviewResponse:
    """让真实 LLM 故事策划继续一轮结构化访谈。"""
    try:
        return await story_service.interview(
            conversation=[item.model_dump() for item in request.conversation],
            design_brief=request.design_brief,
        )
    except StoryGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/drafts", response_model=StoryDraftResponse, status_code=201)
async def create_story_draft(request: StoryDraftRequest) -> StoryDraftResponse:
    """把玩家已确认设计稿编译为通过校验的限时 Canon 草稿。"""
    try:
        return await story_service.create_draft_via_task(request.design_brief)
    except StoryGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/drafts/{draft_id}/publish",
    response_model=StoryPublishResponse,
    status_code=201,
)
async def publish_story(draft_id: str) -> StoryPublishResponse:
    """将限时草稿原子发布到 canon 目录并立即注册。"""
    return StoryPublishResponse(story=await story_service.publish(draft_id))


@router.post(
    "/generation-tasks",
    response_model=StoryGenerationTaskResponse,
    status_code=202,
)
async def create_story_generation_task(
    request: StoryDraftRequest,
) -> StoryGenerationTaskResponse:
    """提交可恢复的分阶段故事生成任务。"""
    return await story_service.create_generation_task(request.design_brief)


@router.get(
    "/generation-tasks/{task_id}",
    response_model=StoryGenerationTaskResponse,
)
async def get_story_generation_task(task_id: str) -> StoryGenerationTaskResponse:
    """返回公开进度、脱敏错误以及完成后的限时草稿。"""
    return story_service.get_generation_task(task_id)


@router.delete(
    "/generation-tasks/{task_id}",
    response_model=StoryGenerationTaskResponse,
)
async def cancel_story_generation_task(task_id: str) -> StoryGenerationTaskResponse:
    """请求在最近安全阶段边界取消生成。"""
    return story_service.cancel_generation_task(task_id)
