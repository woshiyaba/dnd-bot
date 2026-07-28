"""故事广场、访谈与 Canon 发布接口模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class StoryConversationMessage(BaseModel):
    """一次故事访谈中的用户或策划消息。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class StoryQuestion(BaseModel):
    """故事策划本轮需要玩家决定的一项问题。"""

    id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1)
    why_it_matters: str = ""
    suggested_options: list[str] = Field(default_factory=list, max_length=4)
    allow_free_text: bool = True


class StoryInterviewRequest(BaseModel):
    """无状态故事访谈请求；客户端携带完整历史和上一版设计稿。"""

    conversation: list[StoryConversationMessage] = Field(min_length=1, max_length=100)
    design_brief: dict[str, Any] = Field(default_factory=dict)


class StoryInterviewResponse(BaseModel):
    """LLM 故事策划输出的结构化访谈结果。"""

    status: Literal["needs_clarification", "ready_for_confirmation", "confirmed"]
    assistant_message: str = Field(min_length=1)
    design_brief: dict[str, Any]
    questions: list[StoryQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_status_shape(self) -> "StoryInterviewResponse":
        """保证状态、问题和玩家确认标记彼此一致。"""
        if self.status == "needs_clarification" and not self.questions:
            raise ValueError("needs_clarification 必须包含至少一个问题")
        if self.status != "needs_clarification" and self.questions:
            raise ValueError(f"{self.status} 状态不能继续携带问题")
        confirmed = self.design_brief.get("user_confirmed") is True
        if self.status == "confirmed" and not confirmed:
            raise ValueError("confirmed 状态必须设置 user_confirmed=true")
        if self.status != "confirmed" and confirmed:
            raise ValueError("未确认状态不能设置 user_confirmed=true")
        return self


class StoryDraftRequest(BaseModel):
    """用玩家已确认的设计稿生成可发布 Canon。"""

    design_brief: dict[str, Any]


class StorySummary(BaseModel):
    """故事广场可公开展示的剧本摘要，不包含剧情秘密。"""

    campaign_id: str
    title: str
    premise: str
    theme: str
    tone: str
    duration_minutes: int
    recommended_player_count: int
    gameplay_focus: list[str]
    content_warnings: list[str]
    beat_count: int


class StoryDraftResponse(BaseModel):
    """已通过确定性校验、等待玩家发布的限时草稿。"""

    draft_id: str
    expires_at: datetime
    story: StorySummary


class StoryPublishResponse(BaseModel):
    """成功写入 canon 目录后的发布结果。"""

    story: StorySummary
