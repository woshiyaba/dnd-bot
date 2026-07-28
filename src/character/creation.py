"""玩家角色创建目录与服务端权威卡面生成。"""

from __future__ import annotations

from typing import Any

from src.character.features import (
    armor_class_from_features,
    class_feature_catalog,
    unlocked_feature_ids,
)
from src.character.skills import learned_skills_for_class
from src.model.combatant import ability_modifier, proficiency_bonus_for_level

ABILITY_IDS = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)
ABILITY_NAMES = {
    "strength": "力量",
    "dexterity": "敏捷",
    "constitution": "体质",
    "intelligence": "智力",
    "wisdom": "感知",
    "charisma": "魅力",
}

RACE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "dwarf": {
        "name": "矮人",
        "bonuses": {"constitution": 2},
        "choice_count": 0,
        "size": "medium",
        "speed": "25ft",
        "proficiencies": ["战斧", "手斧", "轻锤", "战锤"],
    },
    "human": {
        "name": "人类",
        "bonuses": {ability: 1 for ability in ABILITY_IDS},
        "choice_count": 0,
        "size": "medium",
        "speed": "30ft",
        "proficiencies": [],
    },
    "dragonborn": {
        "name": "龙裔",
        "bonuses": {"strength": 2, "charisma": 1},
        "choice_count": 0,
        "size": "medium",
        "speed": "30ft",
        "proficiencies": [],
    },
    "half_elf": {
        "name": "半精灵",
        "bonuses": {"charisma": 2},
        "choice_count": 2,
        "choice_excludes": ["charisma"],
        "size": "medium",
        "speed": "30ft",
        "proficiencies": [],
    },
    "elf": {
        "name": "精灵",
        "bonuses": {"dexterity": 2},
        "choice_count": 0,
        "size": "medium",
        "speed": "30ft",
        "proficiencies": ["长剑", "短剑", "短弓", "长弓"],
    },
    "tiefling": {
        "name": "提夫林",
        "bonuses": {"intelligence": 1, "charisma": 2},
        "choice_count": 0,
        "size": "medium",
        "speed": "30ft",
        "proficiencies": [],
    },
}

CLASS_DEFINITIONS: dict[str, dict[str, Any]] = {
    "barbarian": {
        "name": "野蛮人",
        "description": "带着原始力量作战的凶猛战士。",
        "hit_die": 12,
        "primary_abilities": ["strength"],
        "save_proficiencies": ["strength", "constitution"],
        "armor_proficiencies": ["轻甲", "中甲", "盾牌"],
        "weapon_proficiencies": ["简易武器", "军用武器"],
        "armor": "无甲",
        "equipment": ["巨斧", "手斧×2", "探索套组", "标枪×4"],
        "weapon": {
            "name": "巨斧",
            "ability": "strength",
            "damage_die": "1d12",
            "damage_type": "slashing",
            "range": "melee",
        },
        "color": "#c65f3d",
    },
    "bard": {
        "name": "吟游诗人",
        "description": "以创生之乐鼓舞同伴的魔法师。",
        "hit_die": 8,
        "primary_abilities": ["charisma"],
        "save_proficiencies": ["dexterity", "charisma"],
        "armor_proficiencies": ["轻甲"],
        "weapon_proficiencies": ["简易武器", "手弩", "长剑", "刺剑", "短剑"],
        "armor": "皮甲",
        "equipment": ["刺剑", "大使套组", "鲁特琴", "皮甲", "匕首"],
        "weapon": {
            "name": "刺剑",
            "ability": "dexterity",
            "damage_die": "1d8",
            "damage_type": "piercing",
            "range": "melee",
        },
        "color": "#9b6ade",
    },
    "cleric": {
        "name": "牧师",
        "description": "操控神圣魔法、服务于更强大力量的斗士。",
        "hit_die": 8,
        "primary_abilities": ["wisdom"],
        "save_proficiencies": ["wisdom", "charisma"],
        "armor_proficiencies": ["轻甲", "中甲", "盾牌"],
        "weapon_proficiencies": ["简易武器"],
        "armor": "鳞甲与盾牌",
        "equipment": [
            "硬头锤",
            "鳞甲",
            "轻弩与弩矢×20",
            "祭司套组",
            "盾牌",
            "圣徽",
        ],
        "weapon": {
            "name": "硬头锤",
            "ability": "strength",
            "damage_die": "1d6",
            "damage_type": "bludgeoning",
            "range": "melee",
        },
        "color": "#c9922a",
    },
    "fighter": {
        "name": "战士",
        "description": "掌握各种武器与护甲的武术大师。",
        "hit_die": 10,
        "primary_abilities": ["strength", "dexterity"],
        "save_proficiencies": ["strength", "constitution"],
        "armor_proficiencies": ["轻甲", "中甲", "重甲", "盾牌"],
        "weapon_proficiencies": ["简易武器", "军用武器"],
        "armor": "链甲与盾牌",
        "equipment": ["链甲", "长剑", "盾牌", "轻弩与弩矢×20", "地城套组"],
        "weapon": {
            "name": "长剑",
            "ability": "strength",
            "damage_die": "1d8",
            "damage_type": "slashing",
            "range": "melee",
        },
        "color": "#4a90d9",
    },
    "paladin": {
        "name": "圣武士",
        "description": "宣誓为神圣誓言献身的神圣勇士。",
        "hit_die": 10,
        "primary_abilities": ["strength", "charisma"],
        "save_proficiencies": ["wisdom", "charisma"],
        "armor_proficiencies": ["轻甲", "中甲", "重甲", "盾牌"],
        "weapon_proficiencies": ["简易武器", "军用武器"],
        "armor": "链甲与盾牌",
        "equipment": ["长剑", "盾牌", "标枪×5", "祭司套组", "链甲", "圣徽"],
        "weapon": {
            "name": "长剑",
            "ability": "strength",
            "damage_die": "1d8",
            "damage_type": "slashing",
            "range": "melee",
        },
        "color": "#d8a53a",
    },
}

_POINT_BUY_COSTS = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}


def point_buy_cost(abilities: dict[str, int]) -> int:
    """计算标准 5e 六属性购点成本，并拒绝不完整或越界输入。"""
    if set(abilities) != set(ABILITY_IDS):
        raise ValueError("必须提交完整的六项基础属性")
    try:
        return sum(_POINT_BUY_COSTS[int(abilities[key])] for key in ABILITY_IDS)
    except KeyError as exc:
        raise ValueError("购点基础属性必须处于 8 到 15") from exc


def apply_race_bonuses(
    race_id: str,
    base_abilities: dict[str, int],
    racial_bonus_choices: list[str],
) -> dict[str, int]:
    """校验种族可选加成并返回种族加成后的最终属性。"""
    race = RACE_DEFINITIONS.get(race_id)
    if race is None:
        raise ValueError("未知种族")
    choice_count = int(race.get("choice_count", 0))
    choices = list(racial_bonus_choices)
    if len(choices) != choice_count or len(set(choices)) != len(choices):
        raise ValueError(f"该种族必须选择 {choice_count} 项不同的额外属性")
    excluded = set(race.get("choice_excludes", []))
    if any(choice not in ABILITY_IDS or choice in excluded for choice in choices):
        raise ValueError("种族额外属性选择无效")
    result = {key: int(base_abilities[key]) for key in ABILITY_IDS}
    for ability, amount in race["bonuses"].items():
        result[ability] += int(amount)
    for ability in choices:
        result[ability] += 1
    return result


def _damage_expression(damage_die: str, modifier: int) -> str:
    if modifier > 0:
        return f"{damage_die}+{modifier}"
    if modifier < 0:
        return f"{damage_die}{modifier}"
    return damage_die


def _armor_class(class_id: str, abilities: dict[str, int]) -> int:
    feature_value = armor_class_from_features(class_id, 1, abilities)
    if feature_value is not None:
        return feature_value
    dexterity = ability_modifier(abilities["dexterity"])
    if class_id == "bard":
        return 11 + dexterity
    if class_id == "cleric":
        return 16 + min(2, dexterity)
    return 18


def build_character_card(
    *,
    character_id: str,
    name: str,
    race_id: str,
    class_id: str,
    base_abilities: dict[str, int],
    racial_bonus_choices: list[str] | None = None,
) -> dict[str, Any]:
    """从不可信创建草稿生成完整、可直接进入战斗的 1 级角色卡。"""
    if point_buy_cost(base_abilities) != 27:
        raise ValueError("六项属性必须恰好使用 27 点购点额度")
    class_definition = CLASS_DEFINITIONS.get(class_id)
    if class_definition is None:
        raise ValueError("未知职业")
    race = RACE_DEFINITIONS.get(race_id)
    if race is None:
        raise ValueError("未知种族")
    abilities = apply_race_bonuses(race_id, base_abilities, racial_bonus_choices or [])
    level = 1
    constitution_modifier = ability_modifier(abilities["constitution"])
    max_hp = max(1, int(class_definition["hit_die"]) + constitution_modifier)
    weapon = class_definition["weapon"]
    attack_modifier = ability_modifier(abilities[str(weapon["ability"])])
    attack_bonus = proficiency_bonus_for_level(level) + attack_modifier
    skills = learned_skills_for_class(class_id, level)
    card: dict[str, Any] = {
        "id": character_id,
        "name": name,
        **abilities,
        "base_abilities": {key: int(base_abilities[key]) for key in ABILITY_IDS},
        "race_id": race_id,
        "race": race["name"],
        "class_id": class_id,
        "char_class": class_definition["name"],
        "level": level,
        "experience": 0,
        "pending_ability_points": 0,
        "current_hp": max_hp,
        "max_hp": max_hp,
        "hit_die": int(class_definition["hit_die"]),
        "size": race["size"],
        "ac": _armor_class(class_id, abilities),
        "initiative_bonus": ability_modifier(abilities["dexterity"]),
        "speed": race["speed"],
        "color": class_definition["color"],
        "equipment": list(class_definition["equipment"]),
        "save_proficiencies": list(class_definition["save_proficiencies"]),
        "armor_proficiencies": list(class_definition["armor_proficiencies"]),
        "weapon_proficiencies": list(
            dict.fromkeys(
                [
                    *class_definition["weapon_proficiencies"],
                    *race.get("proficiencies", []),
                ]
            )
        ),
        "features": unlocked_feature_ids(class_id, level),
        "attacks": [
            {
                "name": weapon["name"],
                "attack_bonus": attack_bonus,
                "damage_dice": _damage_expression(
                    str(weapon["damage_die"]), attack_modifier
                ),
                "damage_type": weapon["damage_type"],
                "range": weapon["range"],
            }
        ],
        "skills": [skill.to_dict() for skill in skills],
        "inventory": [],
    }
    return card


def character_creation_catalog() -> dict[str, Any]:
    """返回前端创建角色所需的稳定、无运行时状态目录。"""
    return {
        "abilities": [
            {"id": ability, "name": ABILITY_NAMES[ability]} for ability in ABILITY_IDS
        ],
        "point_buy": {
            "budget": 27,
            "minimum": 8,
            "maximum": 15,
            "costs": {str(score): cost for score, cost in _POINT_BUY_COSTS.items()},
        },
        "races": [
            {"id": race_id, **definition}
            for race_id, definition in RACE_DEFINITIONS.items()
        ],
        "classes": [
            {
                "id": class_id,
                "name": definition["name"],
                "description": definition["description"],
                "hit_die": definition["hit_die"],
                "primary_abilities": definition["primary_abilities"],
                "save_proficiencies": definition["save_proficiencies"],
                "armor_proficiencies": definition["armor_proficiencies"],
                "weapon_proficiencies": definition["weapon_proficiencies"],
                "armor": definition["armor"],
                "equipment": definition["equipment"],
                "weapon_name": definition["weapon"]["name"],
                "features": class_feature_catalog(class_id),
                "color": definition["color"],
            }
            for class_id, definition in CLASS_DEFINITIONS.items()
        ],
    }
