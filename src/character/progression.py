"""经验、升级、属性提升与等级派生能力规则。"""

from __future__ import annotations

from typing import Any

from src.character.creation import ABILITY_IDS, CLASS_DEFINITIONS
from src.character.features import armor_class_from_features, unlocked_feature_ids
from src.character.skills import learned_skills_for_class
from src.model.combatant import Character, ability_modifier

XP_THRESHOLDS: tuple[int, ...] = (
    0,
    300,
    900,
    2700,
    6500,
    14000,
    23000,
    34000,
    48000,
    64000,
    85000,
    100000,
    120000,
    140000,
    165000,
    195000,
    225000,
    265000,
    305000,
    355000,
)
ABILITY_INCREASE_LEVELS = {4, 8, 12, 16, 19}


def level_for_experience(experience: int) -> int:
    """按标准累计经验阈值返回 1–20 级。"""
    xp = max(0, int(experience))
    level = 1
    for index, threshold in enumerate(XP_THRESHOLDS, start=1):
        if xp >= threshold:
            level = index
        else:
            break
    return min(level, 20)


def next_level_experience(level: int) -> int | None:
    """返回升到下一级所需累计经验；20 级返回 None。"""
    normalized = max(1, min(int(level), 20))
    if normalized >= 20:
        return None
    return XP_THRESHOLDS[normalized]


def grant_experience(character: Character, amount: int) -> dict[str, Any]:
    """发放经验、自动升级 HP/技能，并返回结构化成长摘要。"""
    granted = max(0, int(amount))
    old_level = character.level
    character.experience += granted
    # 旧存档可能已有等级、但没有累计经验字段。经验系统接入后不能因此降级；
    # 它们会在累计经验追上当前等级阈值后继续按标准表成长。
    new_level = max(old_level, level_for_experience(character.experience))
    hp_gain = 0
    unlocked: list[str] = []
    was_alive = character.is_alive
    for level in range(old_level + 1, new_level + 1):
        gain = max(
            1,
            character.hit_die // 2 + 1 + ability_modifier(character.constitution),
        )
        character.max_hp += gain
        if was_alive:
            character.current_hp += gain
        hp_gain += gain
        if level in ABILITY_INCREASE_LEVELS:
            character.pending_ability_points += 2
    character.level = new_level

    existing = {skill.skill_id for skill in character.skills}
    for skill in learned_skills_for_class(character.class_id or "", new_level):
        if skill.skill_id not in existing:
            character.skills.append(skill)
            existing.add(skill.skill_id)
            unlocked.append(skill.skill_id)

    for feature_id in unlocked_feature_ids(character.class_id or "", new_level):
        if feature_id not in character.features:
            character.features.append(feature_id)
            unlocked.append(feature_id)
    refresh_character_combat_stats(character)
    return {
        "experience_gained": granted,
        "experience": character.experience,
        "old_level": old_level,
        "new_level": new_level,
        "hp_gained": hp_gain,
        "unlocked": unlocked,
        "pending_ability_points": character.pending_ability_points,
    }


def apply_ability_increases(
    character: Character, increases: dict[str, int]
) -> dict[str, int]:
    """应用一轮属性提升；仅接受 +2 单项或两个 +1，最终值不超过 20。"""
    if character.pending_ability_points < 2:
        raise ValueError("角色当前没有待分配的属性提升")
    normalized = {key: int(value) for key, value in increases.items() if value}
    if any(key not in ABILITY_IDS for key in normalized):
        raise ValueError("包含未知属性")
    values = sorted(normalized.values())
    if values not in ([2], [1, 1]):
        raise ValueError("属性提升必须是一项 +2 或两项各 +1")
    for ability, amount in normalized.items():
        current = int(getattr(character, ability))
        if current + amount > 20:
            raise ValueError("属性提升后的单项属性不能超过 20")
    old_constitution_modifier = ability_modifier(character.constitution)
    for ability, amount in normalized.items():
        setattr(character, ability, int(getattr(character, ability)) + amount)
    constitution_delta = (
        ability_modifier(character.constitution) - old_constitution_modifier
    )
    if constitution_delta:
        hp_delta = constitution_delta * character.level
        character.max_hp += hp_delta
        if character.is_alive:
            character.current_hp = min(
                character.max_hp, character.current_hp + hp_delta
            )
    refresh_character_combat_stats(character)
    character.pending_ability_points -= 2
    return {ability: int(getattr(character, ability)) for ability in ABILITY_IDS}


def refresh_character_combat_stats(character: Character) -> None:
    """属性、等级变化后重算默认装备产生的 AC、先攻与主武器数值。"""
    definition = CLASS_DEFINITIONS.get(character.class_id or "")
    if definition is None:
        return
    dexterity_modifier = ability_modifier(character.dexterity)
    abilities = {ability: int(getattr(character, ability)) for ability in ABILITY_IDS}
    feature_ac = armor_class_from_features(
        character.class_id or "",
        character.level,
        abilities,
        character.features,
    )
    if feature_ac is not None:
        base_ac = feature_ac
    elif character.class_id == "bard":
        base_ac = 11 + dexterity_modifier
    elif character.class_id == "cleric":
        base_ac = 16 + min(2, dexterity_modifier)
    else:
        base_ac = 18
    active_ac_modifiers = sum(
        condition.amount for condition in character.conditions if condition.stat == "ac"
    )
    character.ac = base_ac + active_ac_modifiers
    character.initiative_bonus = dexterity_modifier
    if not character.attacks:
        return
    weapon = definition["weapon"]
    attack_modifier = ability_modifier(int(getattr(character, str(weapon["ability"]))))
    character.attacks[0].attack_bonus = character.proficiency_bonus + attack_modifier
    base_die = str(weapon["damage_die"])
    character.attacks[0].damage_dice = (
        f"{base_die}+{attack_modifier}"
        if attack_modifier > 0
        else f"{base_die}{attack_modifier}" if attack_modifier < 0 else base_die
    )
