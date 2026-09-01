"""故事访谈、分阶段生成任务与 Canon 发布接口模型。"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import re
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

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

    @model_validator(mode="after")
    def validate_prompt_size(self) -> "StoryInterviewRequest":
        """限制一次访谈进入模型的总文本规模。"""
        total = sum(len(item.content) for item in self.conversation)
        total += len(self.design_brief.model_dump_json())
        if total > 32_000:
            raise ValueError("故事访谈内容总长度不能超过 32000 字符")
        return self


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

    @model_validator(mode="after")
    def validate_prompt_size(self) -> "StoryDraftRequest":
        """限制确认稿进入生成管线的总文本规模。"""
        if len(self.design_brief.model_dump_json()) > 16_000:
            raise ValueError("故事设计稿总长度不能超过 16000 字符")
        return self


# ---------------------------------------------------------------------------
# CanonDraft：完整 Canon 的 LLM 结构化输出边界。
# 跨 ID 引用、可达性与唯一 owner 仍由 story/model 层的确定性校验负责。
# ---------------------------------------------------------------------------
class CanonDraftModel(BaseModel):
    """Canon 草稿公共配置：禁止模型发明引擎未实现的字段。"""

    model_config = ConfigDict(extra="forbid")


class CanonTriggerPredicateDraft(CanonDraftModel):
    """所有 Trigger kind 共用的封闭 predicate 字段集合。"""

    flag: str | None = None
    equals: bool | None = None
    all: list[str] | None = None
    any: list[str] | None = None
    item_id: str | None = None
    location_id: str | None = None
    outcome: Literal["players_win", "players_lose"] | None = None
    encounter_id: str | None = None
    prompt: str | None = None
    action: str | None = None


class CanonTriggerDraft(CanonDraftModel):
    """剧情推进或整局胜负条件。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["flag", "item", "location", "combat_outcome", "semantic", "action"]
    predicate: CanonTriggerPredicateDraft
    description: str


class CanonAttackDraft(CanonDraftModel):
    """固定 CombatCard 中的一种攻击。"""

    name: str = Field(min_length=1)
    attack_bonus: int
    damage_dice: str = Field(min_length=1)
    damage_type: Literal[
        "slashing",
        "piercing",
        "bludgeoning",
        "acid",
        "cold",
        "fire",
        "force",
        "lightning",
        "necrotic",
        "poison",
        "psychic",
        "radiant",
        "thunder",
    ]
    range: Literal["melee", "ranged"]


class CanonCombatCardDraft(CanonDraftModel):
    """Canon actor 可直接交给战斗引擎加载的固定卡面。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    strength: int
    dexterity: int
    constitution: int
    intelligence: int
    wisdom: int
    charisma: int
    current_hp: int = Field(ge=1)
    max_hp: int = Field(ge=1)
    ac: int = Field(ge=1)
    initiative_bonus: int
    attacks: list[CanonAttackDraft] = Field(min_length=1)


class CanonDeathFallbackDraft(CanonDraftModel):
    """关键 NPC 死亡后的剧情续接。"""

    guidance: str = Field(min_length=1)
    consequence: str = Field(min_length=1)
    stuck_hint: str = ""


class CanonNpcDraft(CanonDraftModel):
    """重要 NPC、普通敌人与 Boss 的 Canon 册页。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    role: str
    goal: str
    secret: str
    disposition: Literal["friendly", "neutral", "hostile"]
    story_critical: bool = False
    death_fallback: CanonDeathFallbackDraft | None = None
    card: CanonCombatCardDraft | None = None


class CanonLocationDraft(CanonDraftModel):
    """一处主要地点及拍内互通出口。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    description: str
    intra_exits: list[str]


class CanonEntryActorDraft(CanonDraftModel):
    """进入 Beat 时已经在场的 actor。"""

    actor_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    disposition: Literal["friendly", "neutral", "hostile"]
    location_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["npc", "monster"]
    card: CanonCombatCardDraft | None = None


class CanonEntryStateDraft(CanonDraftModel):
    """进入 Beat 时用于搭建场景的冻结状态。"""

    location_id: str | None
    preserve_current_scene: bool = False
    description: str = ""
    actors: list[CanonEntryActorDraft]
    exits: list[str]
    threat: str | None = None


class CanonGrantedItemDraft(CanonDraftModel):
    """发现线索后由引擎原子发放的物品。"""

    item_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    quantity: int = Field(default=1, ge=1)
    recipient: Literal["active_actor"] = "active_actor"


class CanonDiscoveryEffectsDraft(CanonDraftModel):
    """KeyInfo 被实际发现后允许提交的世界效果。"""

    flags_set: dict[str, bool] = Field(default_factory=dict)
    grant_items: list[CanonGrantedItemDraft] = Field(default_factory=list)


class CanonKeyInfoDraft(CanonDraftModel):
    """一条绑定到明确地点的可发现线索。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str = Field(min_length=1)
    location_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    discovery_hints: list[str] = Field(min_length=1)
    discovery_effects: CanonDiscoveryEffectsDraft | None = None


class CanonEncounterDraft(CanonDraftModel):
    """Beat 内可直接交给战斗子图的遭遇模板。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    location_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    monster_ids: list[str] = Field(min_length=1)
    surprised: list[str] = Field(default_factory=list)
    loot_table: list[str] = Field(default_factory=list)
    xp_reward: int = Field(default=0, ge=0)
    random_seed: int | None = None
    on_win_flags: list[str] = Field(default_factory=list)
    on_win_discoveries: list[str] = Field(default_factory=list)


class CanonExitDraft(CanonDraftModel):
    """Trigger 命中后的跨 Beat 出口。"""

    trigger_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    next_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class CanonStuckFallbackDraft(CanonDraftModel):
    """玩家卡关时允许 DM 使用的预写提示。"""

    hint: str = ""
    reveal_clue: bool = False
    point_to_exit: str | None = None


class CanonBeatDraft(CanonDraftModel):
    """完整可运行的剧情 Beat。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    kind: Literal["opening", "exploration", "conflict", "climax", "ending"]
    act_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    estimated_minutes: int = Field(ge=1, le=120)
    objective: str
    pressure: str
    relevant_clue_ids: list[str] = Field(default_factory=list)
    payoff_flag_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(min_length=1)
    entry_state: CanonEntryStateDraft
    key_info: list[CanonKeyInfoDraft]
    advance_conditions: list[CanonTriggerDraft]
    exits: list[CanonExitDraft]
    stuck_fallback: CanonStuckFallbackDraft
    encounter: CanonEncounterDraft | None = None
    ending_outcome: Literal["win", "lose"] | None = None


class CanonActionRequirementsDraft(CanonDraftModel):
    """规则行动可用范围的静态限制。"""

    flags: list[str] = Field(default_factory=list)
    beat_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    encounter_ids: list[str] = Field(default_factory=list)


class CanonActionTargetingDraft(CanonDraftModel):
    """规则行动的目标选择边界。"""

    faction: Literal["self", "ally", "enemy", "any"] | None = None
    life_state: Literal["alive", "down", "any"] | None = None
    range: Literal["melee", "any"] | None = None
    actor_ids: list[str] = Field(default_factory=list)
    min_targets: int = Field(default=0, ge=0)
    max_targets: int = Field(default=0, ge=0)


class CanonActionUsageDraft(CanonDraftModel):
    """规则行动的确定性消耗方式。"""

    kind: Literal[
        "unlimited",
        "skill_resource",
        "consume_item",
        "once_per_combat",
        "once_per_session",
    ]
    item_id: str | None = None
    quantity: int = Field(default=1, ge=1)


class CanonActionCheckDraft(CanonDraftModel):
    """ActionDefinition 中由引擎执行的检定模板。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["attack_roll", "saving_throw", "ability_check"]
    roller: Literal["actor", "target"]
    target_mode: Literal["selected_each", "selected_one", "actor", "none"]
    ability: (
        Literal[
            "strength",
            "dexterity",
            "constitution",
            "intelligence",
            "wisdom",
            "charisma",
        ]
        | None
    ) = None
    bonus_source: Literal["ability", "weapon_attack", "spell_attack"] | None = None
    dc_source: Literal["spell_save"] | None = None
    fixed_dc: int | None = Field(default=None, ge=1, le=30)


class CanonActionWhenDraft(CanonDraftModel):
    """检定结果与效果模板之间的分支条件。"""

    check_template_id: str | None = None
    outcomes: list[
        Literal["success", "failure", "hit", "miss", "critical", "always"]
    ] = Field(min_length=1)


class CanonActionEffectDraft(CanonDraftModel):
    """引擎支持的战斗或世界效果模板。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal[
        "damage",
        "healing",
        "temporary_hp",
        "add_condition",
        "remove_condition",
        "modify_ac",
        "modify_attack_bonus",
        "move_zone",
        "revive",
        "set_flag",
        "grant_item",
        "remove_item",
        "discover_clue",
        "move_location",
        "transition_beat",
    ]
    target_mode: Literal["selected_each", "selected_one", "actor", "none"]
    dice: str | None = None
    amount: int | None = Field(default=None, ge=-500, le=500)
    amount_bonus_source: Literal["spellcasting_modifier", "actor_level"] | None = None
    multiplier: float | None = Field(default=None, ge=0)
    damage_type: (
        Literal[
            "slashing",
            "piercing",
            "bludgeoning",
            "acid",
            "cold",
            "fire",
            "force",
            "lightning",
            "necrotic",
            "poison",
            "psychic",
            "radiant",
            "thunder",
        ]
        | None
    ) = None
    condition: (
        Literal[
            "prone",
            "poisoned",
            "restrained",
            "stunned",
            "damage_over_time",
            "blinded",
            "charmed",
            "deafened",
            "frightened",
            "grappled",
            "incapacitated",
            "invisible",
            "paralyzed",
            "petrified",
            "unconscious",
            "buff",
            "debuff",
        ]
        | None
    ) = None
    rounds: int | None = Field(default=None, ge=1)
    target_zone: str | None = None
    flag: str | None = None
    value: bool | None = None
    item_id: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    clue_id: str | None = None
    location_id: str | None = None
    beat_id: str | None = None
    when: CanonActionWhenDraft


class CanonActionContractDraft(CanonDraftModel):
    """LLM 只能实例化、不能改写的行动机械模板。"""

    concentration: bool = False
    check_templates: list[CanonActionCheckDraft]
    effect_templates: list[CanonActionEffectDraft] = Field(min_length=1)


class CanonActionDefinitionDraft(CanonDraftModel):
    """物品、技能或任务特性的统一规则行动定义。"""

    schema_version: Literal[2] = 2
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    source_kind: Literal["skill", "item", "quest_feature"]
    source_ref: str = Field(min_length=1)
    scopes: list[Literal["combat", "world"]] = Field(min_length=1, max_length=2)
    description: str = ""
    requirements: CanonActionRequirementsDraft
    targeting: CanonActionTargetingDraft
    usage: CanonActionUsageDraft
    contract: CanonActionContractDraft


class CanonDraft(CanonDraftModel):
    """LLM 编译完成、可直接交给 ``Canon.from_dict`` 的完整 JSON 草稿。"""

    campaign_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    theme: str = Field(min_length=1)
    tone: str = Field(min_length=1)
    duration_minutes: int = Field(ge=10, le=120)
    length_mode: LengthMode
    act_count: int = Field(ge=1, le=5)
    runtime_location_scoping: Literal[True]
    recommended_player_count: int = Field(ge=1, le=6)
    gameplay_focus: list[str] = Field(min_length=1)
    content_warnings: list[str]
    declared_flags: list[str]
    action_definitions: list[CanonActionDefinitionDraft]
    win_condition: CanonTriggerDraft
    lose_condition: CanonTriggerDraft
    cast: list[CanonNpcDraft]
    locations: list[CanonLocationDraft] = Field(min_length=1)
    start_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    beats: list[CanonBeatDraft] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 连贯性复核：约束 staged generator 的复核报告与定向 Act 修复输出。
# ---------------------------------------------------------------------------
class StoryContinuityIssue(BaseModel):
    """连贯性复核发现的一项脱敏问题。"""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1)
    affected_act_ids: list[str]


class StoryContinuityReview(BaseModel):
    """LLM 连贯性复核的完整结构化报告。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    issues: list[StoryContinuityIssue]

    @model_validator(mode="after")
    def validate_passed(self) -> "StoryContinuityReview":
        """passed 必须与是否存在 error 级问题保持一致。"""
        has_errors = any(issue.severity == "error" for issue in self.issues)
        if self.passed == has_errors:
            raise ValueError("passed 必须等于不存在 error 级 issues")
        return self


class CanonActFragmentDraft(CanonDraftModel):
    """连贯性定向修复可替换的单个完整 Act 分片。"""

    beats: list[CanonBeatDraft] = Field(min_length=1)


def continuity_repair_schema(
    affected_act_ids: set[str] | list[str],
) -> type[BaseModel]:
    """为本轮连贯性修复创建只允许受影响 Act 的输出边界。"""
    return _continuity_repair_schema(tuple(sorted(set(affected_act_ids))))


@lru_cache(maxsize=None)
def _continuity_repair_schema(affected_act_ids: tuple[str, ...]) -> type[BaseModel]:
    """缓存动态模型，并把允许返回的 Act ID 固化为字段。"""
    if not affected_act_ids:
        raise ValueError("连贯性修复至少需要一个受影响 Act")
    invalid = [
        act_id for act_id in affected_act_ids if not _ID_PATTERN.fullmatch(act_id)
    ]
    if invalid:
        raise ValueError("连贯性修复包含非法 Act ID：" + "、".join(invalid))

    suffix = "_".join(affected_act_ids)
    act_fragments_model = create_model(
        f"ContinuityRepairActFragments_{suffix}",
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        **{
            f"act_fragment_{index}": (
                CanonActFragmentDraft,
                Field(alias=act_id),
            )
            for index, act_id in enumerate(affected_act_ids, start=1)
        },
    )
    return create_model(
        f"ContinuityRepair_{suffix}",
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        act_fragments=(act_fragments_model, ...),
    )


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


class StoryScaleProfileCandidate(BaseModel):
    """允许省略将由确认稿整体覆盖的规模字段。"""

    model_config = ConfigDict(extra="forbid")

    playable_beats: int | None = None
    acts: int | None = None
    locations: int | None = None
    encounters: int | None = None
    clues: int | None = None


class PlanActCandidate(PlanAct):
    """允许省略由 Beat 唯一推导字段的 Act 候选。"""

    estimated_minutes: int | None = None
    beat_ids: list[str] | None = None


class PlanBeatCandidate(PlanBeat):
    """允许省略由伏笔账本唯一推导字段的 Beat 候选。"""

    payoff_flag_ids: list[str] | None = None


class PlanBranchPointCandidate(PlanBranchPoint):
    """允许省略由源 Beat 出口唯一推导的 choices。"""

    choices: list[str] | None = None


class StoryPlanCandidate(BaseModel):
    """LLM 输出边界模型；归一化并完整校验前不可进入业务流程。"""

    model_config = ConfigDict(extra="forbid")

    plan_version: int = Field(default=1, ge=1)
    campaign_id_candidate: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    start_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    scale_profile: StoryScaleProfileCandidate | None = None
    acts: list[PlanActCandidate] = Field(min_length=1)
    beats: list[PlanBeatCandidate] = Field(min_length=1)
    entities: PlanEntities
    clue_graph: list[PlanClueLink] = Field(default_factory=list)
    branch_points: list[PlanBranchPointCandidate] = Field(default_factory=list)
    foreshadowing_payoffs: list[PlanPayoff] = Field(default_factory=list)
    ending_routes: list[PlanEndingRoute] = Field(min_length=2, max_length=2)
    effect_owner_ledger: list[EffectOwner] = Field(default_factory=list)


def story_plan_section_repair_schema(
    section_names: set[str],
) -> type[BaseModel]:
    """为本轮 StoryPlan 局部修复创建精确的结构化输出边界。"""
    return _story_plan_section_repair_schema(tuple(sorted(section_names)))


@lru_cache(maxsize=None)
def _story_plan_section_repair_schema(
    section_names: tuple[str, ...],
) -> type[BaseModel]:
    """缓存动态模型，避免每次故事生成重复创建相同区段组合。"""
    section_fields: dict[str, tuple[Any, Any]] = {
        "scale_profile": (StoryScaleProfileCandidate, ...),
        "acts": (list[PlanActCandidate], Field(min_length=1)),
        "beats": (list[PlanBeatCandidate], Field(min_length=1)),
        "entities": (PlanEntities, ...),
        "clue_graph": (list[PlanClueLink], ...),
        "branch_points": (list[PlanBranchPointCandidate], ...),
        "foreshadowing_payoffs": (list[PlanPayoff], ...),
        "ending_routes": (
            list[PlanEndingRoute],
            Field(min_length=2, max_length=2),
        ),
        "effect_owner_ledger": (list[EffectOwner], ...),
    }
    unknown = sorted(set(section_names) - set(section_fields))
    if unknown:
        raise ValueError("StoryPlan 局部修复包含未知区段：" + "、".join(unknown))
    if not section_names:
        raise ValueError("StoryPlan 局部修复至少需要一个区段")

    suffix = "_".join(section_names)
    sections_model = create_model(
        f"StoryPlanRepairSections_{suffix}",
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        **{name: section_fields[name] for name in section_names},
    )
    return create_model(
        f"StoryPlanSectionRepair_{suffix}",
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        repair_kind=(Literal["story_plan_sections"], ...),
        sections=(sections_model, ...),
    )


# ---------------------------------------------------------------------------
# StoryPlan 渐进生成：仅用于生成器的小型封闭产物。
# ---------------------------------------------------------------------------
PlanItemT = TypeVar("PlanItemT")


class PlanBatch(BaseModel, Generic[PlanItemT]):
    """带代码锁定目标的小批次；具体阶段再收紧到 3 或 5 项。"""

    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1, max_length=128)
    items: list[PlanItemT] = Field(default_factory=list, max_length=5)


class PlanComplexBatch(PlanBatch[PlanItemT], Generic[PlanItemT]):
    model_config = ConfigDict(extra="forbid")

    items: list[PlanItemT] = Field(default_factory=list, max_length=3)


class PlanSimpleBatch(PlanBatch[PlanItemT], Generic[PlanItemT]):
    model_config = ConfigDict(extra="forbid")

    items: list[PlanItemT] = Field(default_factory=list, max_length=5)


class PlanFrameAct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    purpose: str = Field(min_length=1)
    turning_point: str = Field(min_length=1)
    playable_beat_count: int = Field(ge=1, le=12)


class StoryPlanFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id_candidate: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    acts: list[PlanFrameAct] = Field(min_length=1, max_length=5)


class PlanBeatOutline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["opening", "exploration", "conflict", "climax"]
    estimated_minutes: int = Field(ge=1, le=120)
    objective: str = Field(min_length=1)


class PlanBranchBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    choice_beat_ids: list[str] = Field(min_length=2, max_length=2)
    reconverge_at: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    distinct_consequences: list[str] = Field(min_length=2, max_length=2)


class PlanBeatDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    pressure: str = Field(min_length=1)
    dramatic_question: str = ""
    entry_hook: str = ""
    fail_forward: str = ""


class PlanEntityBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actors: int = Field(ge=1, le=24)
    flags: int = Field(ge=0, le=12)
    items: int = Field(ge=0, le=12)
    actions: int = Field(ge=0, le=12)
    payoffs: int = Field(ge=0, le=12)


class PlanEntityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class PlanBeatPlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    location_ids: list[str] = Field(min_length=1)
    actor_ids: list[str] = Field(default_factory=list)
    clue_ids: list[str] = Field(default_factory=list)
    encounter_id: str | None = None


class PlanRouteText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_summary: str = Field(min_length=1)
    consequence: str = Field(min_length=1)


class PlanClueDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clue_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    answers: str = Field(min_length=1)
    unlocks: list[str] = Field(default_factory=list)
    alternative_approaches: list[str] = Field(min_length=2)


class PlanPayoffDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flag_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    setup_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    payoff_beat_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)


class PlanEndingDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ending_id: Literal["ending_win", "ending_lose"]
    objective: str = Field(min_length=1)
    required_facts: list[str] = Field(default_factory=list)
    payoffs: list[str] = Field(default_factory=list)


class PlanEffectOwnerChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    owner_kind: Literal[
        "discovery", "encounter_win", "initial_state", "rule_action", "dm_free_write"
    ]
    owner_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class StoryPlanWorkState(BaseModel):
    """只累计已经通过阶段契约的 StoryPlan 小产物。"""

    model_config = ConfigDict(extra="forbid")

    frame: StoryPlanFrame | None = None
    beat_outlines: list[PlanBeatOutline] = Field(default_factory=list)
    branch_blueprints: list[PlanBranchBlueprint] = Field(default_factory=list)
    beat_details: list[PlanBeatDetail] = Field(default_factory=list)
    entity_budget: PlanEntityBudget | None = None
    entities: PlanEntities = Field(default_factory=PlanEntities)
    placements: list[PlanBeatPlacement] = Field(default_factory=list)
    routes: dict[str, list[PlanRouteText]] = Field(default_factory=dict)
    clues: list[PlanClueDetail] = Field(default_factory=list)
    payoffs: list[PlanPayoffDetail] = Field(default_factory=list)
    endings: list[PlanEndingDetail] = Field(default_factory=list)
    owners: list[PlanEffectOwnerChoice] = Field(default_factory=list)


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
    llm_calls_used: int = Field(default=0, ge=0)
    llm_calls_limit: int = Field(default=24, ge=1)
    error: str | None = None
    draft: StoryDraftResponse | None = None


class StoryPublishResponse(BaseModel):
    """成功写入 canon 目录后的发布结果。"""

    story: StorySummary
