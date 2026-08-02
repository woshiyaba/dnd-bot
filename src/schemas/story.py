"""故事访谈、分阶段生成任务与 Canon 发布接口模型。"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LengthMode = Literal["short", "standard", "long"]
GenerationStatus = Literal[
    "queued", "running", "completed", "failed", "cancel_requested", "cancelled"
]

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_LENGTH_LIMITS: dict[str, dict[str, tuple[int, int]]] = {
    "short": {
        "duration": (10, 30),
        "playable_beats": (3, 4),
        "acts": (2, 3),
        "locations": (3, 5),
        "encounters": (1, 2),
        "clues": (2, 4),
    },
    "standard": {
        "duration": (31, 60),
        "playable_beats": (5, 7),
        "acts": (3, 4),
        "locations": (5, 8),
        "encounters": (2, 3),
        "clues": (4, 7),
    },
    "long": {
        "duration": (61, 120),
        "playable_beats": (8, 12),
        "acts": (4, 5),
        "locations": (8, 14),
        "encounters": (3, 5),
        "clues": (7, 12),
    },
}
_MIN_BRANCH_POINTS: dict[str, int] = {"short": 0, "standard": 1, "long": 2}


def infer_length_mode(duration_minutes: int | None) -> LengthMode:
    """按确认时长推导单 Session 规模档位。"""
    duration = int(duration_minutes or 20)
    if duration <= 30:
        return "short"
    if duration <= 60:
        return "standard"
    return "long"


def length_limits(length_mode: str) -> dict[str, tuple[int, int]]:
    """返回规模档位的只读约束副本。"""
    return dict(_LENGTH_LIMITS[length_mode])


def minimum_branch_points(length_mode: str) -> int:
    """返回规模档位要求的最少汇流分支数。"""
    return _MIN_BRANCH_POINTS[length_mode]


class StoryConversationMessage(BaseModel):
    """一次故事访谈中的用户或策划消息。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class StoryQuestion(BaseModel):
    """故事策划本轮需要玩家决定的一项问题。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    question: str = Field(min_length=1)
    why_it_matters: str = ""
    suggested_options: list[str] = Field(default_factory=list, max_length=4)
    allow_free_text: bool = True


class StoryScaleProfile(BaseModel):
    """玩家确认的可量化故事规模；数值是目标而不是宽泛建议。"""

    model_config = ConfigDict(extra="forbid")

    playable_beats: int = Field(ge=1, le=12)
    acts: int = Field(ge=1, le=5)
    locations: int = Field(ge=1, le=14)
    encounters: int = Field(ge=0, le=5)
    clues: int = Field(ge=0, le=12)


class StoryBranchingBudget(BaseModel):
    """有限分支与汇流预算。"""

    model_config = ConfigDict(extra="forbid")

    meaningful_branch_points: int = Field(default=0, ge=0, le=4)
    max_parallel_beats: int = Field(default=1, ge=1, le=3)
    reconverge_before_climax: bool = True


class StoryPacing(BaseModel):
    """五段节奏百分比；确认稿必须合计 100。"""

    model_config = ConfigDict(extra="forbid")

    opening_percent: int = Field(default=10, ge=0, le=100)
    exploration_social_percent: int = Field(default=45, ge=0, le=100)
    escalation_percent: int = Field(default=20, ge=0, le=100)
    climax_percent: int = Field(default=20, ge=0, le=100)
    ending_percent: int = Field(default=5, ge=0, le=100)

    @model_validator(mode="after")
    def validate_total(self) -> "StoryPacing":
        total = (
            self.opening_percent
            + self.exploration_social_percent
            + self.escalation_percent
            + self.climax_percent
            + self.ending_percent
        )
        if total != 100:
            raise ValueError("pacing 百分比合计必须等于 100")
        return self


class StorySideContent(BaseModel):
    """支线数量与收束约束。"""

    model_config = ConfigDict(extra="forbid")

    desired_side_threads: int = Field(default=0, ge=0, le=3)
    must_resolve_before_ending: bool = True


def _default_scale(length_mode: str) -> StoryScaleProfile:
    limits = length_limits(length_mode)
    return StoryScaleProfile(
        playable_beats=limits["playable_beats"][0],
        acts=limits["acts"][0],
        locations=limits["locations"][0],
        encounters=limits["encounters"][0],
        clues=limits["clues"][0],
    )


def _default_branching(length_mode: str) -> StoryBranchingBudget:
    minimum = minimum_branch_points(length_mode)
    return StoryBranchingBudget(
        meaningful_branch_points=minimum,
        max_parallel_beats=1 if length_mode == "short" else 2,
    )


class StoryDesignBrief(BaseModel):
    """可渐进填写、确认时执行完整约束的严格访谈设计稿。"""

    model_config = ConfigDict(extra="forbid")

    contract_version: int = Field(default=2, ge=1)
    revision: int | None = Field(default=None, ge=1)
    confirmed_revision: int | None = Field(default=None, ge=1)
    working_title: str | None = None
    premise: str | None = None
    player_role: str | None = None
    core_conflict: str | None = None
    antagonist_direction: str | None = None
    gameplay_focus: list[str] = Field(default_factory=list)
    tone: str | None = None
    content_boundaries: list[str] = Field(default_factory=list)
    content_warnings: list[str] = Field(default_factory=list)
    duration_minutes: int | None = Field(default=None, ge=10, le=120)
    player_count: int | None = Field(default=None, ge=1, le=6)
    ending_direction: str | None = None
    must_have: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    system_design_freedom: list[str] = Field(default_factory=list)
    length_mode: LengthMode | None = None
    target_sessions: int = Field(default=1, ge=1)
    scale_profile: StoryScaleProfile | None = None
    branching_style: Literal["linear", "branch_and_reconverge"] | None = None
    branching_budget: StoryBranchingBudget | None = None
    pacing: StoryPacing | None = None
    set_pieces: list[str] = Field(default_factory=list)
    side_content: StorySideContent | None = None
    failure_style: str | None = None
    replayability: Literal["low", "medium", "high"] | None = None
    user_confirmed: bool = False

    @model_validator(mode="after")
    def derive_and_validate_scale(self) -> "StoryDesignBrief":
        """兼容旧短篇稿；一旦确认，就把规模契约补齐并严格核对。"""
        validation_errors: list[str] = []
        expected_length_mode = self.length_mode
        if self.duration_minutes is not None:
            inferred = infer_length_mode(self.duration_minutes)
            expected_length_mode = inferred
            if self.length_mode is None:
                self.length_mode = inferred
            elif self.user_confirmed and self.length_mode != inferred:
                validation_errors.append(
                    "length_mode 与 duration_minutes 所属档位不一致"
                )
        if self.length_mode is not None:
            self.scale_profile = self.scale_profile or _default_scale(self.length_mode)
            self.branching_budget = self.branching_budget or _default_branching(
                self.length_mode
            )
            self.branching_style = self.branching_style or (
                "linear" if self.length_mode == "short" else "branch_and_reconverge"
            )
        self.pacing = self.pacing or StoryPacing()
        self.side_content = self.side_content or StorySideContent()
        self.failure_style = self.failure_style or "fail_forward_with_cost"
        self.replayability = self.replayability or "medium"

        # 访谈阶段允许暂存尚待纠正的方向；只有确认稿才执行完整跨字段契约。
        if self.user_confirmed and self.target_sessions != 1:
            validation_errors.append("当前仅支持 target_sessions=1")
        if self.user_confirmed and expected_length_mode and self.scale_profile:
            limits = length_limits(expected_length_mode)
            values = self.scale_profile.model_dump()
            for name, value in values.items():
                lower, upper = limits[name]
                if not lower <= value <= upper:
                    validation_errors.append(
                        f"scale_profile.{name} 必须在 {lower} 到 {upper} 之间"
                    )
            minimum_branches = minimum_branch_points(expected_length_mode)
            if (
                self.branching_budget
                and self.branching_budget.meaningful_branch_points < minimum_branches
            ):
                validation_errors.append(
                    f"{expected_length_mode} 至少需要 {minimum_branches} 次汇流分支"
                )
            if (
                self.branching_budget
                and (
                    self.branching_budget.meaningful_branch_points > 0
                    or minimum_branches > 0
                )
                and self.branching_style != "branch_and_reconverge"
            ):
                validation_errors.append(
                    "存在分支预算时 branching_style 必须为 branch_and_reconverge"
                )
        if validation_errors:
            raise ValueError("；".join(validation_errors))
        return self


class StoryInterviewRequest(BaseModel):
    """无状态故事访谈请求；客户端携带完整历史和上一版设计稿。"""

    conversation: list[StoryConversationMessage] = Field(min_length=1, max_length=100)
    design_brief: StoryDesignBrief = Field(default_factory=StoryDesignBrief)


class StoryInterviewResponse(BaseModel):
    """LLM 故事策划输出的结构化访谈结果。"""

    status: Literal["needs_clarification", "ready_for_confirmation", "confirmed"]
    assistant_message: str = Field(min_length=1)
    design_brief: StoryDesignBrief
    questions: list[StoryQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_status_shape(self) -> "StoryInterviewResponse":
        """保证状态、问题和玩家确认标记彼此一致。"""
        if self.status == "needs_clarification" and not self.questions:
            raise ValueError("needs_clarification 必须包含至少一个问题")
        if self.status != "needs_clarification" and self.questions:
            raise ValueError(f"{self.status} 状态不能继续携带问题")
        confirmed = self.design_brief.user_confirmed
        if self.status == "confirmed" and not confirmed:
            raise ValueError("confirmed 状态必须设置 user_confirmed=true")
        if self.status != "confirmed" and confirmed:
            raise ValueError("未确认状态不能设置 user_confirmed=true")
        return self


class StoryDraftRequest(BaseModel):
    """用玩家已确认的设计稿生成可发布 Canon。"""

    design_brief: StoryDesignBrief


# ---------------------------------------------------------------------------
# StoryPlan：只在生成阶段持久化，不作为公开草稿内容返回。
# ---------------------------------------------------------------------------
class PlanEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = ""
    summary: str = ""


class PlanEntities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actors: list[PlanEntity] = Field(default_factory=list)
    locations: list[PlanEntity] = Field(default_factory=list)
    encounters: list[PlanEntity] = Field(default_factory=list)
    clues: list[PlanEntity] = Field(default_factory=list)
    flags: list[PlanEntity] = Field(default_factory=list)
    items: list[PlanEntity] = Field(default_factory=list)
    actions: list[PlanEntity] = Field(default_factory=list)


class PlanAct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    purpose: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=1, le=120)
    beat_ids: list[str] = Field(min_length=1)
    turning_point: str = Field(min_length=1)


class PlanExit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    condition_summary: str = Field(min_length=1)
    consequence: str = Field(min_length=1)


class PlanBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    act_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["opening", "exploration", "conflict", "climax", "ending"]
    estimated_minutes: int = Field(ge=1, le=120)
    objective: str = Field(min_length=1)
    pressure: str = Field(min_length=1)
    dramatic_question: str = ""
    entry_hook: str = ""
    location_ids: list[str] = Field(default_factory=list)
    actor_ids: list[str] = Field(default_factory=list)
    clue_ids: list[str] = Field(default_factory=list)
    encounter_id: str | None = None
    exits: list[PlanExit] = Field(default_factory=list)
    fail_forward: str = ""
    payoff_flag_ids: list[str] = Field(default_factory=list)


class PlanClueLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clue_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    answers: str = Field(min_length=1)
    unlocks: list[str] = Field(default_factory=list)
    acquisition_owner: str = Field(min_length=1)
    alternative_approaches: list[str] = Field(default_factory=list)


class PlanBranchPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    choices: list[str] = Field(min_length=2, max_length=3)
    distinct_consequences: list[str] = Field(min_length=2)
    reconverge_at: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class PlanPayoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    setup_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    payoff_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)


class PlanEndingRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ending_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    outcome: Literal["win", "lose"]
    required_facts: list[str] = Field(default_factory=list)
    payoffs: list[str] = Field(default_factory=list)


class EffectOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    effect_kind: Literal["flag", "item"]
    owner_kind: Literal[
        "discovery", "encounter_win", "initial_state", "rule_action", "dm_free_write"
    ]
    owner_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class StoryPlan(BaseModel):
    """通过确定性图校验后才允许进入 Canon 分片编译的内部计划。"""

    model_config = ConfigDict(extra="forbid")

    plan_version: int = Field(default=1, ge=1)
    campaign_id_candidate: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    start_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    scale_profile: StoryScaleProfile
    acts: list[PlanAct] = Field(min_length=1)
    beats: list[PlanBeat] = Field(min_length=1)
    entities: PlanEntities
    clue_graph: list[PlanClueLink] = Field(default_factory=list)
    branch_points: list[PlanBranchPoint] = Field(default_factory=list)
    foreshadowing_payoffs: list[PlanPayoff] = Field(default_factory=list)
    ending_routes: list[PlanEndingRoute] = Field(min_length=2, max_length=2)
    effect_owner_ledger: list[EffectOwner] = Field(default_factory=list)


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


class StoryQualityMetrics(BaseModel):
    """可公开的结构指标与脱敏质量结果。"""

    act_count: int
    playable_beat_count: int
    location_count: int
    clue_count: int
    encounter_count: int
    branch_count: int
    semantic_trigger_count: int
    shortest_minutes: int
    longest_minutes: int
    repair_count: int = 0
    continuity_passed: bool = False
    quality_notes: list[str] = Field(default_factory=list)


class StoryDraftResponse(BaseModel):
    """已通过全部校验、等待玩家发布的限时草稿。"""

    draft_id: str
    expires_at: datetime
    story: StorySummary
    quality: StoryQualityMetrics | None = None


class StoryGenerationTaskResponse(BaseModel):
    """可轮询、可跨进程恢复的故事生成任务公开状态。"""

    task_id: str
    status: GenerationStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    draft: StoryDraftResponse | None = None


class StoryPublishResponse(BaseModel):
    """成功写入 canon 目录后的发布结果。"""

    story: StorySummary
