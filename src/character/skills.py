"""职业法术与职业主动能力的只读目录。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.character.features import (
    active_skill_definition,
    active_skill_definitions,
    skill_cooldown_for,
)
from src.model.effects import LearnedSkill

_ROOT = Path(__file__).resolve().parents[2] / "dnd_skill"
_SPELLCASTING_CLASSES = {"bard", "cleric", "paladin"}
_COMBAT_TYPES = {
    "damage",
    "healing",
    "control",
    "defense",
    "buff",
    "mobility",
    "summoning",
}


@lru_cache(maxsize=1)
def spell_catalog() -> dict[str, dict[str, Any]]:
    """加载共享法术目录，并按稳定法术 ID 建立索引。"""
    raw = json.loads((_ROOT / "spells.json").read_text(encoding="utf-8"))
    return {str(item["id"]): dict(item) for item in raw}


@lru_cache(maxsize=None)
def class_spell_ids(class_id: str) -> dict[int, tuple[str, ...]]:
    """读取某职业按最低环位分组的法术 ID。"""
    if class_id not in _SPELLCASTING_CLASSES:
        return {}
    raw = json.loads(
        (_ROOT / "classes" / f"{class_id}.json").read_text(encoding="utf-8")
    )
    return {
        int(level): tuple(str(item["id"]) for item in entries)
        for level, entries in raw["spells_by_level"].items()
    }


def max_spell_level(class_id: str, character_level: int) -> int:
    """按全施法/半施法等级表返回当前可用最高环位。"""
    level = max(1, min(int(character_level), 20))
    if class_id in {"bard", "cleric"}:
        return min(9, (level + 1) // 2)
    if class_id == "paladin":
        return 0 if level < 2 else min(5, (level + 3) // 4)
    return -1


def unlocked_spell_ids(class_id: str, character_level: int) -> list[str]:
    """返回职业当前等级自动掌握的全部法术 ID。"""
    groups = class_spell_ids(class_id)
    maximum = max_spell_level(class_id, character_level)
    result: list[str] = list(groups.get(0, ()))
    if maximum >= 1:
        for spell_level in range(1, maximum + 1):
            result.extend(groups.get(spell_level, ()))
    return result


def spell_unlock_character_level(class_id: str, spell_level: int) -> int:
    """把法术环位换算成当前开放职业首次解锁它的角色等级。"""
    level = max(0, int(spell_level))
    if level == 0:
        return 1
    if class_id in {"bard", "cleric"}:
        return 2 * level - 1
    if class_id == "paladin":
        return 2 if level == 1 else 4 * level - 3
    raise ValueError(f"职业 «{class_id}» 没有法术解锁表")


def skill_definition(skill_id: str) -> dict[str, Any] | None:
    """读取法术或项目职业主动能力的完整定义。"""
    active_definition = active_skill_definition(skill_id)
    if active_definition is not None:
        return active_definition
    item = spell_catalog().get(skill_id)
    if item is None:
        return None
    definition = dict(item)
    details: list[str] = []
    for key in ("damage_calculation", "healing_calculation"):
        calculation = definition.get(key)
        if isinstance(calculation, dict):
            if calculation.get("details"):
                details.append(str(calculation["details"]))
            if calculation.get("scaling"):
                details.append(str(calculation["scaling"]))
    definition["rules_text"] = "\n".join(details) or (
        f"{definition['name_zh']}；类型：{', '.join(definition.get('types', []))}；"
        f"持续时间：{definition.get('duration', '立即')}。"
    )
    definition["source_type"] = "spell"
    return definition


def learned_skills_for_class(class_id: str, character_level: int) -> list[LearnedSkill]:
    """创建当前等级自动解锁的运行时技能引用。"""
    result: list[LearnedSkill] = []
    for skill_id in unlocked_spell_ids(class_id, character_level):
        definition = skill_definition(skill_id)
        if definition is None:
            raise ValueError(f"法术目录缺少技能 «{skill_id}»")
        cooldown = skill_cooldown_for(
            class_id,
            character_level,
            int(definition.get("cooldown_rounds", 0)),
        )
        result.append(
            LearnedSkill(
                skill_id=skill_id,
                name=str(definition["name_zh"]),
                source_type="spell",
                unlock_level=spell_unlock_character_level(
                    class_id, int(definition.get("level", 0))
                ),
                charges=None,
                cooldown_rounds=cooldown,
                types=tuple(str(value) for value in definition.get("types", [])),
            )
        )
    for definition in reversed(active_skill_definitions(class_id, character_level)):
        result.insert(
            0,
            LearnedSkill(
                skill_id=str(definition["id"]),
                name=str(definition["name_zh"]),
                source_type=str(definition.get("source_type", "active_feature")),
                unlock_level=max(1, int(definition.get("unlock_level", 1))),
                charges=None,
                cooldown_rounds=skill_cooldown_for(
                    class_id,
                    character_level,
                    int(definition.get("cooldown_rounds", 0)),
                ),
                types=tuple(str(value) for value in definition.get("types", [])),
            ),
        )
    return result


def is_combat_skill(skill_id: str) -> bool:
    """判断技能是否应出现在战斗行动面板。"""
    definition = skill_definition(skill_id)
    if definition is None:
        return False
    return bool(set(definition.get("types", [])) & _COMBAT_TYPES)
