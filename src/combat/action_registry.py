"""统一规则行动注册、技能适配与可用性计算。"""

from __future__ import annotations

import re
from typing import Any, Iterable

from src.character.skills import is_combat_skill, skill_definition
from src.combat.rules import in_reach
from src.model.combatant import Character, Combatant
from src.model.enums import Ability
from src.model.rule_action import ActionDefinition

_DICE_PATTERN = re.compile(r"(\d+d(?:4|6|8|10|12|20)(?:[+-]\d+)?)", re.I)
_SAVE_METHODS = {f"{ability.value}_save": ability.value for ability in Ability}
_DAMAGE_WORDS = {
    "强酸": "acid",
    "冷冻": "cold",
    "寒冰": "cold",
    "火焰": "fire",
    "力场": "force",
    "闪电": "lightning",
    "黯蚀": "necrotic",
    "毒素": "poison",
    "心灵": "psychic",
    "光耀": "radiant",
    "雷鸣": "thunder",
    "挥砍": "slashing",
    "穿刺": "piercing",
    "钝击": "bludgeoning",
}


def skill_action_definition(
    actor: Character, skill_id: str
) -> tuple[ActionDefinition | None, str | None]:
    """把常见的结构化技能目录条目适配成规则行动定义。

    无法安全转换的技能返回明确原因，并由行动面板以禁用状态展示。
    """
    raw = skill_definition(skill_id)
    if raw is None:
        return None, "技能目录不存在"
    learned = next((item for item in actor.skills if item.skill_id == skill_id), None)
    if learned is None:
        return None, "角色未掌握该技能"

    if skill_id == "feature_divine_smite":
        effects = [
            {
                "id": "radiant_damage",
                "kind": "damage",
                "target_mode": "selected_one",
                "dice": "2d8",
                "damage_type": "radiant",
                "when": {
                    "check_template_id": "weapon_hit",
                    "outcomes": ["hit", "critical"],
                },
            }
        ]
        if actor.level > 1:
            effects.append(
                {
                    "id": "level_damage",
                    "kind": "damage",
                    "target_mode": "selected_one",
                    "dice": f"{actor.level - 1}d4",
                    "damage_type": "radiant",
                    "when": {
                        "check_template_id": "weapon_hit",
                        "outcomes": ["hit", "critical"],
                    },
                }
            )
        return (
            ActionDefinition.from_dict(
                {
                    "id": f"skill.{skill_id}",
                    "name": learned.name or str(raw.get("name_zh") or skill_id),
                    "source_kind": "skill",
                    "source_ref": skill_id,
                    "scopes": ["combat"],
                    "description": str(raw.get("rules_text") or ""),
                    "targeting": {
                        "faction": "enemy",
                        "life_state": "alive",
                        "range": "melee",
                        "min_targets": 1,
                        "max_targets": 1,
                    },
                    "usage": {"kind": "skill_resource"},
                    "contract": {
                        "check_templates": [
                            {
                                "id": "weapon_hit",
                                "kind": "attack_roll",
                                "roller": "actor",
                                "target_mode": "selected_one",
                                "bonus_source": "weapon_attack",
                            }
                        ],
                        "effect_templates": effects,
                    },
                }
            ),
            None,
        )

    if skill_id == "skill_second_wind":
        return _simple_skill_action(
            actor,
            learned,
            raw,
            targeting={
                "faction": "self",
                "life_state": "alive",
                "min_targets": 1,
                "max_targets": 1,
            },
            effects=[
                {
                    "id": "healing",
                    "kind": "healing",
                    "target_mode": "actor",
                    "dice": "1d10",
                    "amount_bonus_source": "actor_level",
                    "when": {"outcomes": ["always"]},
                }
            ],
        )

    if skill_id == "revivify":
        return _simple_skill_action(
            actor,
            learned,
            raw,
            targeting={
                "faction": "ally",
                "life_state": "down",
                "range": "melee",
                "min_targets": 1,
                "max_targets": 1,
            },
            effects=[
                {
                    "id": "revive",
                    "kind": "revive",
                    "target_mode": "selected_one",
                    "amount": 1,
                    "when": {"outcomes": ["always"]},
                }
            ],
        )

    damage = raw.get("damage_calculation")
    healing = raw.get("healing_calculation")
    if isinstance(damage, dict):
        dice, bonus_source = _calculation_amount(damage)
        if dice is None:
            return None, "伤害公式无法转换为受支持的骰子协议"
        damage_type = _damage_type(str(raw.get("rules_text") or ""))
        if damage_type is None:
            return None, "技能文本没有可确定映射的伤害类型"
        method = str(damage.get("method") or "")
        checks: list[dict[str, Any]] = []
        when = {"outcomes": ["always"]}
        if method == "spell_attack":
            checks.append(
                {
                    "id": "spell_hit",
                    "kind": "attack_roll",
                    "roller": "actor",
                    "target_mode": "selected_each",
                    "bonus_source": "spell_attack",
                }
            )
            when = {"check_template_id": "spell_hit", "outcomes": ["hit", "critical"]}
        elif method in _SAVE_METHODS:
            checks.append(
                {
                    "id": "saving_throw",
                    "kind": "saving_throw",
                    "roller": "target",
                    "target_mode": "selected_each",
                    "ability": _SAVE_METHODS[method],
                    "dc_source": "spell_save",
                }
            )
            when = {"check_template_id": "saving_throw", "outcomes": ["failure"]}
        elif method:
            return None, f"暂不支持技能判定方式 {method}"
        effect = {
            "id": "damage",
            "kind": "damage",
            "target_mode": "selected_each",
            "dice": dice,
            "damage_type": damage_type,
            "when": when,
        }
        if bonus_source:
            effect["amount_bonus_source"] = bonus_source
        effects = [effect]
        if method in _SAVE_METHODS and str(damage.get("on_save") or "") == "half":
            effects.append(
                {
                    **effect,
                    "id": "damage_on_save",
                    "multiplier": 0.5,
                    "when": {
                        "check_template_id": "saving_throw",
                        "outcomes": ["success"],
                    },
                }
            )
        return _simple_skill_action(actor, learned, raw, checks=checks, effects=effects)

    if isinstance(healing, dict):
        dice, bonus_source = _calculation_amount(healing)
        if dice is None:
            return None, "治疗公式无法转换为受支持的骰子协议"
        effect: dict[str, Any] = {
            "id": "healing",
            "kind": "healing",
            "target_mode": "selected_each",
            "dice": dice,
            "when": {"outcomes": ["always"]},
        }
        if bonus_source:
            effect["amount_bonus_source"] = bonus_source
        return _simple_skill_action(
            actor,
            learned,
            raw,
            targeting={
                "faction": "ally",
                "life_state": "alive",
                "min_targets": 1,
                "max_targets": int(raw.get("max_targets", 1)),
            },
            effects=[effect],
        )

    return None, "该技能包含尚未实现的规则原语"


def canon_action_definitions(
    raw_definitions: Iterable[dict[str, Any]],
) -> list[ActionDefinition]:
    """加载当前 Canon 下发的行动定义。"""
    return [ActionDefinition.from_dict(dict(item)) for item in raw_definitions]


def combat_action_entries(
    actor: Combatant,
    combatants: dict[str, Combatant],
    *,
    canon_definitions: Iterable[dict[str, Any]] = (),
    story_flags: dict[str, Any] | None = None,
    encounter_id: str | None = None,
    used_action_ids: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, ActionDefinition]]:
    """返回战斗面板条目与本回合可解析的定义索引。"""
    definitions: list[tuple[ActionDefinition | None, str | None]] = []
    unsupported: list[dict[str, Any]] = []
    if isinstance(actor, Character):
        for skill in actor.skills:
            if not is_combat_skill(skill.skill_id):
                continue
            definition, reason = skill_action_definition(actor, skill.skill_id)
            if definition is None:
                unsupported.append(
                    {
                        "action_id": f"skill.{skill.skill_id}",
                        "name": skill.name or skill.skill_id,
                        "description": "",
                        "source_kind": "skill",
                        "source_ref": skill.skill_id,
                        "enabled": False,
                        "unavailable_reason": reason or "该技能暂不受支持",
                        "min_targets": 0,
                        "max_targets": 0,
                        "targets": [],
                        "usage": {"kind": "skill_resource"},
                    }
                )
            else:
                definitions.append((definition, reason))
    definitions.extend(
        (item, None) for item in canon_action_definitions(canon_definitions)
    )
    entries, index = _action_entries(
        actor,
        combatants,
        definitions,
        scope="combat",
        flags=story_flags or {},
        encounter_id=encounter_id,
        used_action_ids=set(used_action_ids),
    )
    return [*entries, *unsupported], index


def world_action_entries(
    actor: Combatant,
    party: dict[str, Combatant],
    *,
    canon_definitions: Iterable[ActionDefinition],
    flags: dict[str, Any],
    beat_id: str | None,
    location_id: str | None,
    used_action_ids: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, ActionDefinition]]:
    """返回探索阶段面板条目与定义索引。"""
    definitions: list[tuple[ActionDefinition | None, str | None]] = []
    if isinstance(actor, Character):
        for skill in actor.skills:
            definition, reason = skill_action_definition(actor, skill.skill_id)
            if definition is not None and "world" not in definition.scopes:
                continue
            definitions.append((definition, reason))
    definitions.extend((item, None) for item in canon_definitions)
    return _action_entries(
        actor,
        party,
        definitions,
        scope="world",
        flags=flags,
        beat_id=beat_id,
        location_id=location_id,
        used_action_ids=set(used_action_ids),
    )


def _simple_skill_action(
    actor: Character,
    learned: Any,
    raw: dict[str, Any],
    *,
    targeting: dict[str, Any] | None = None,
    checks: list[dict[str, Any]] | None = None,
    effects: list[dict[str, Any]],
) -> tuple[ActionDefinition, None]:
    target = targeting or {
        "faction": "enemy",
        "life_state": "alive",
        "range": "any",
        "min_targets": int(raw.get("min_targets", 1)),
        "max_targets": int(raw.get("max_targets", 1)),
    }
    return (
        ActionDefinition.from_dict(
            {
                "id": f"skill.{learned.skill_id}",
                "name": learned.name or str(raw.get("name_zh") or learned.skill_id),
                "source_kind": "skill",
                "source_ref": learned.skill_id,
                "scopes": ["combat"],
                "description": str(raw.get("rules_text") or ""),
                "targeting": target,
                "usage": {"kind": "skill_resource"},
                "contract": {
                    "check_templates": list(checks or []),
                    "effect_templates": effects,
                    "concentration": bool(raw.get("concentration")),
                },
            }
        ),
        None,
    )


def _calculation_amount(calculation: dict[str, Any]) -> tuple[str | None, str | None]:
    base = calculation.get("base") or []
    text = str(base[0]) if base else ""
    match = _DICE_PATTERN.search(text)
    if match is None:
        return None, None
    bonus = "spellcasting_modifier" if "施法关键属性调整值" in text else None
    return match.group(1).lower(), bonus


def _damage_type(text: str) -> str | None:
    for word, damage_type in _DAMAGE_WORDS.items():
        if word in text:
            return damage_type
    return None


def _action_entries(
    actor: Combatant,
    targets: dict[str, Combatant],
    definitions: Iterable[tuple[ActionDefinition | None, str | None]],
    *,
    scope: str,
    flags: dict[str, Any],
    used_action_ids: set[str],
    encounter_id: str | None = None,
    beat_id: str | None = None,
    location_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, ActionDefinition]]:
    entries: list[dict[str, Any]] = []
    index: dict[str, ActionDefinition] = {}
    for definition, unsupported_reason in definitions:
        if definition is None:
            continue
        if scope not in definition.scopes:
            continue
        index[definition.id] = definition
        reason = unsupported_reason or _requirement_failure(
            definition,
            actor,
            flags=flags,
            encounter_id=encounter_id,
            beat_id=beat_id,
            location_id=location_id,
            used_action_ids=used_action_ids,
        )
        legal_targets = _legal_targets(definition, actor, targets)
        minimum = int(definition.targeting.get("min_targets", 0))
        if reason is None and len(legal_targets) < minimum:
            reason = "当前没有合法目标"
        entries.append(
            {
                "action_id": definition.id,
                "name": definition.name,
                "description": definition.description,
                "source_kind": definition.source_kind,
                "source_ref": definition.source_ref,
                "enabled": reason is None,
                "unavailable_reason": reason,
                "min_targets": minimum,
                "max_targets": int(
                    definition.targeting.get("max_targets", max(1, minimum))
                ),
                "targets": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "faction": item.faction.value,
                        "life_state": item.life_state.value,
                        "zone": item.current_zone,
                    }
                    for item in legal_targets
                ],
                "usage": dict(definition.usage),
            }
        )
    return entries, index


def _requirement_failure(
    definition: ActionDefinition,
    actor: Combatant,
    *,
    flags: dict[str, Any],
    encounter_id: str | None,
    beat_id: str | None,
    location_id: str | None,
    used_action_ids: set[str],
) -> str | None:
    requirements = definition.requirements
    for flag in requirements.get("flags", []):
        if not flags.get(str(flag)):
            return f"需要线索或状态：{flag}"
    if (
        requirements.get("encounter_ids")
        and encounter_id not in requirements["encounter_ids"]
    ):
        return "不适用于当前遭遇"
    if requirements.get("beat_ids") and beat_id not in requirements["beat_ids"]:
        return "不适用于当前剧情阶段"
    if (
        requirements.get("location_ids")
        and location_id not in requirements["location_ids"]
    ):
        return "不适用于当前地点"
    usage_kind = str(definition.usage.get("kind") or "unlimited")
    if (
        usage_kind in {"once_per_combat", "once_per_session"}
        and definition.id in used_action_ids
    ):
        return "本次冒险中已使用"
    if definition.source_kind == "skill":
        if not isinstance(actor, Character):
            return "只有角色可以使用技能"
        learned = next(
            (item for item in actor.skills if item.skill_id == definition.source_ref),
            None,
        )
        if learned is None or not learned.is_available:
            return "技能充能不足或正在冷却"
    if definition.source_kind == "item" or usage_kind == "consume_item":
        item_id = str(definition.usage.get("item_id") or definition.source_ref)
        if not isinstance(actor, Character) or not any(
            item.item_id == item_id and item.is_available for item in actor.inventory
        ):
            return "背包中没有该物品"
    return None


def _legal_targets(
    definition: ActionDefinition, actor: Combatant, targets: dict[str, Combatant]
) -> list[Combatant]:
    target_rule = definition.targeting
    faction = str(target_rule.get("faction") or "any")
    life_state = str(target_rule.get("life_state") or "any")
    distance = str(target_rule.get("range") or "any")
    result: list[Combatant] = []
    actor_ids = {str(value) for value in target_rule.get("actor_ids", [])}
    for target in targets.values():
        if actor_ids and target.id not in actor_ids:
            continue
        if faction == "self" and target.id != actor.id:
            continue
        if faction == "enemy" and target.faction == actor.faction:
            continue
        if faction == "ally" and target.faction != actor.faction:
            continue
        if life_state == "alive" and not target.is_alive:
            continue
        if life_state == "down" and target.is_alive:
            continue
        if distance == "melee" and not in_reach(actor, target, False):
            continue
        result.append(target)
    return result
