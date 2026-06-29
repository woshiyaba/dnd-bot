"""战斗数据模型包。

对外暴露枚举、攻击/状态/技能/道具、参战者继承体系与图状态。
卡面字段沿用英文键，便于落库与前端对接。
"""

from src.model.attack import Attack
from src.model.canon import (
    Beat,
    BeatKind,
    Canon,
    Encounter,
    EndingOutcome,
    Exit,
    KeyInfo,
    LocationSpec,
    NpcSpec,
    Trigger,
    TriggerKind,
    beat_brief,
    evaluate_trigger,
    validate_canon,
)
from src.model.combat_state import (
    CombatState,
    load_combatant,
    load_combatants,
)
from src.model.combatant import (
    Character,
    Combatant,
    Monster,
    NPC,
    PlayerCharacter,
    ability_modifier,
    proficiency_bonus_for_level,
)
from src.model.effects import Condition, InventoryItem, LearnedSkill
from src.model.enums import (
    Ability,
    ActionType,
    CombatOutcome,
    CombatPhase,
    ConditionType,
    DamageType,
    Faction,
    InterruptType,
    LifeState,
    Range,
)

__all__ = [
    # 枚举
    "Ability",
    "DamageType",
    "Range",
    "LifeState",
    "ConditionType",
    "Faction",
    "CombatPhase",
    "CombatOutcome",
    "ActionType",
    "InterruptType",
    # 组件模型
    "Attack",
    "Condition",
    "LearnedSkill",
    "InventoryItem",
    # 参战者继承体系
    "Combatant",
    "Monster",
    "Character",
    "PlayerCharacter",
    "NPC",
    "ability_modifier",
    "proficiency_bonus_for_level",
    # 图状态
    "CombatState",
    "load_combatant",
    "load_combatants",
    # 剧情圣经（故事系统）
    "Canon",
    "Beat",
    "Trigger",
    "Exit",
    "KeyInfo",
    "NpcSpec",
    "LocationSpec",
    "Encounter",
    "BeatKind",
    "TriggerKind",
    "EndingOutcome",
    "validate_canon",
    "evaluate_trigger",
    "beat_brief",
]
