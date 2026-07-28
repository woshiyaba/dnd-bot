"""职业特性注册表与可复用机械 hook。"""

from __future__ import annotations

from typing import Any, Iterable

FEATURE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "feature_unarmored_defense": {
        "id": "feature_unarmored_defense",
        "name": "无甲防御",
        "description": "未着甲时，AC 等于 10 + 敏捷调整值 + 体质调整值。",
        "class_id": "barbarian",
        "unlock_level": 1,
        "hooks": {
            "armor_class": {
                "base": 10,
                "abilities": ["dexterity", "constitution"],
            }
        },
    },
    "feature_font_of_inspiration": {
        "id": "feature_font_of_inspiration",
        "name": "激励之源",
        "description": "吟游诗人的技能不进入冷却。",
        "class_id": "bard",
        "unlock_level": 1,
        "hooks": {"skill_cooldown_override": 0},
    },
    "feature_domain_spells": {
        "id": "feature_domain_spells",
        "name": "领域法术",
        "description": "自动学习当前等级可用的全部牧师技能。",
        "class_id": "cleric",
        "unlock_level": 1,
        "hooks": {"spell_learning": "all_eligible"},
    },
    "feature_action_surge": {
        "id": "feature_action_surge",
        "name": "动作如潮",
        "description": "常驻增加行动次数，并在 5 级和 11 级继续提高。",
        "class_id": "fighter",
        "unlock_level": 1,
        "hooks": {
            "general_action_bonus": [
                {"level": 1, "amount": 1},
                {"level": 5, "amount": 2},
                {"level": 11, "amount": 3},
            ]
        },
    },
    "feature_divine_smite": {
        "id": "feature_divine_smite",
        "name": "至圣斩",
        "description": "命中敌人后造成 2d8 光耀伤害，每个后续等级再增加 1d4。",
        "class_id": "paladin",
        "unlock_level": 1,
        "hooks": {
            "active_skill": {
                "id": "feature_divine_smite",
                "name_zh": "至圣斩",
                "name_en": "Divine Smite",
                "level": 0,
                "types": ["damage"],
                "max_targets": 1,
                "target_scope": "same_zone_enemy_alive",
                "cooldown_rounds": 1,
                "concentration": False,
                "duration": "立即",
                "rules_text": (
                    "以近战武器命中一个敌人，造成 2d8 光耀伤害；"
                    "角色每升一级额外增加 1d4。"
                ),
                "source_type": "active_feature",
            }
        },
    },
    "feature_extra_attack": {
        "id": "feature_extra_attack",
        "name": "额外攻击",
        "description": "5 级起，执行攻击动作后可以再攻击一次。",
        "class_id": None,
        "unlock_level": 5,
        "hooks": {
            "extra_attack_bonus": [{"level": 5, "amount": 1}],
        },
    },
}


def unlocked_feature_ids(class_id: str, level: int) -> list[str]:
    """返回指定职业和等级应拥有的全部职业/通用特性 ID。"""
    normalized_level = max(1, int(level))
    return [
        feature_id
        for feature_id, definition in FEATURE_DEFINITIONS.items()
        if definition.get("class_id") in {None, class_id}
        and normalized_level >= int(definition["unlock_level"])
    ]


def class_feature_catalog(class_id: str) -> list[dict[str, Any]]:
    """返回角色创建界面可展示的该职业特性摘要。"""
    return [
        {
            "id": definition["id"],
            "name": definition["name"],
            "description": definition["description"],
            "unlock_level": definition["unlock_level"],
        }
        for definition in FEATURE_DEFINITIONS.values()
        if definition.get("class_id") == class_id
    ]


def _active_feature_ids(
    class_id: str, level: int, owned_features: Iterable[str] = ()
) -> list[str]:
    """合并卡面特性与按等级应解锁特性，兼容旧存档缺少特性列表。"""
    active = set(owned_features) | set(unlocked_feature_ids(class_id, level))
    return [feature_id for feature_id in FEATURE_DEFINITIONS if feature_id in active]


def _tiered_bonus_for(
    hook_name: str, class_id: str, level: int, owned_features: Iterable[str] = ()
) -> int:
    """汇总多个特性在指定等级生效的分段数值 hook。"""
    bonus = 0
    for feature_id in _active_feature_ids(class_id, level, owned_features):
        tiers = (
            FEATURE_DEFINITIONS.get(feature_id, {}).get("hooks", {}).get(hook_name, [])
        )
        eligible = [
            int(tier["amount"]) for tier in tiers if int(level) >= int(tier["level"])
        ]
        if eligible:
            bonus += eligible[-1]
    return bonus


def general_action_budget_for(
    class_id: str, level: int, owned_features: Iterable[str] = ()
) -> int:
    """返回可用于任意行动的基础动作与职业额外动作总数。"""
    return 1 + _tiered_bonus_for(
        "general_action_bonus", class_id, level, owned_features
    )


def extra_attack_budget_for(
    class_id: str, level: int, owned_features: Iterable[str] = ()
) -> int:
    """返回执行过攻击动作后可继续使用的额外攻击次数。"""
    return _tiered_bonus_for("extra_attack_bonus", class_id, level, owned_features)


def action_budget_for(
    class_id: str, level: int, owned_features: Iterable[str] = ()
) -> int:
    """返回角色全部可用行动的展示上限。"""
    return general_action_budget_for(
        class_id, level, owned_features
    ) + extra_attack_budget_for(class_id, level, owned_features)


def armor_class_from_features(
    class_id: str,
    level: int,
    abilities: dict[str, int],
    owned_features: Iterable[str] = (),
) -> int | None:
    """执行首个 armor_class hook；没有覆盖公式时返回 None。"""
    for feature_id in _active_feature_ids(class_id, level, owned_features):
        hook = (
            FEATURE_DEFINITIONS.get(feature_id, {}).get("hooks", {}).get("armor_class")
        )
        if not hook:
            continue
        modifiers = sum(
            (int(abilities[ability]) - 10) // 2 for ability in hook["abilities"]
        )
        return int(hook["base"]) + modifiers
    return None


def skill_cooldown_for(
    class_id: str,
    level: int,
    base_cooldown: int,
    owned_features: Iterable[str] = (),
) -> int:
    """应用 skill_cooldown_override hook，返回技能的实际基础冷却。"""
    cooldown = max(0, int(base_cooldown))
    for feature_id in _active_feature_ids(class_id, level, owned_features):
        hooks = FEATURE_DEFINITIONS.get(feature_id, {}).get("hooks", {})
        if "skill_cooldown_override" in hooks:
            cooldown = int(hooks["skill_cooldown_override"])
    return cooldown


def active_skill_definitions(class_id: str, level: int) -> list[dict[str, Any]]:
    """返回当前等级由职业特性注入的主动技能定义。"""
    result: list[dict[str, Any]] = []
    for feature_id in unlocked_feature_ids(class_id, level):
        feature = FEATURE_DEFINITIONS[feature_id]
        definition = feature.get("hooks", {}).get("active_skill")
        if definition:
            active = dict(definition)
            active.setdefault("unlock_level", int(feature["unlock_level"]))
            result.append(active)
    return result


def active_skill_definition(skill_id: str) -> dict[str, Any] | None:
    """按技能 ID 查找任一职业特性提供的主动技能定义。"""
    for feature in FEATURE_DEFINITIONS.values():
        definition = feature.get("hooks", {}).get("active_skill")
        if definition and definition.get("id") == skill_id:
            active = dict(definition)
            active.setdefault("unlock_level", int(feature["unlock_level"]))
            return active
    return None
