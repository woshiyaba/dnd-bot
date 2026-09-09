"""统一规则行动注册、技能适配与可用性计算。"""

from __future__ import annotations

from typing import Any, Iterable

from src.character.skills import is_combat_skill, skill_definition
from src.character.inventory import HEALING_POTION
from src.combat.rules import in_reach
from src.model.combatant import Character, Combatant
from src.model.rule_action import ActionDefinition

_SUPPORTED_SKILL_IDS = frozenset(
    {
        "feature_divine_smite",
        "sacred_flame",
        "cure_wounds",
        "revivify",
        "healing_word",
        "inflict_wounds",
        "mass_cure_wounds",
        "mass_healing_word",
        "harm",
        "heal",
    }
)


def skill_action_definition(
    actor: Character, skill_id: str
) -> tuple[ActionDefinition | None, str | None]:
    """把规则已完整建模的技能转换成行动定义。

    白名单外技能返回明确原因，并由行动面板以禁用状态展示。
    """
    raw = skill_definition(skill_id)
    if raw is None:
        return None, "技能目录不存在"
    learned = next((item for item in actor.skills if item.skill_id == skill_id), None)
    if learned is None:
        return None, "角色未掌握该技能"
    if skill_id not in _SUPPORTED_SKILL_IDS:
        return None, "该技能尚未完成规则建模"

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

    targeting, checks, effects = _skill_action_contract(actor, skill_id)
    return _simple_skill_action(
        learned,
        raw,
        targeting=targeting,
        checks=checks,
        effects=effects,
    )


def _skill_action_contract(
    actor: Character, skill_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """返回十项白名单技能的完整机械合同。"""
    ally_one = {
        "faction": "ally",
        "life_state": "any",
        "range": "any",
        "min_targets": 1,
        "max_targets": 1,
    }
    ally_six = {**ally_one, "max_targets": 6}
    enemy_one = {
        "faction": "enemy",
        "life_state": "alive",
        "range": "any",
        "min_targets": 1,
        "max_targets": 1,
    }
    always = {"outcomes": ["always"]}

    if skill_id == "revivify":
        return (
            {**ally_one, "life_state": "down", "range": "melee"},
            [],
            [
                {
                    "id": "revive",
                    "kind": "revive",
                    "target_mode": "selected_one",
                    "amount": 1,
                    "when": always,
                }
            ],
        )

    healing = {
        "cure_wounds": ("1d8", "melee", 1),
        "healing_word": ("1d4", "any", 1),
        "mass_cure_wounds": ("3d8", "any", 6),
        "mass_healing_word": ("1d4", "any", 6),
    }
    if skill_id in healing:
        dice, range_kind, maximum = healing[skill_id]
        return (
            {**(ally_six if maximum == 6 else ally_one), "range": range_kind},
            [],
            [
                {
                    "id": "healing",
                    "kind": "healing",
                    "target_mode": "selected_each",
                    "dice": dice,
                    "amount_bonus_source": "spellcasting_modifier",
                    "when": always,
                }
            ],
        )

    if skill_id == "heal":
        return (
            ally_one,
            [],
            [
                {
                    "id": "healing",
                    "kind": "healing",
                    "target_mode": "selected_one",
                    "amount": 70,
                    "when": always,
                }
            ],
        )

    if skill_id == "inflict_wounds":
        return (
            {**enemy_one, "range": "melee"},
            [
                {
                    "id": "spell_hit",
                    "kind": "attack_roll",
                    "roller": "actor",
                    "target_mode": "selected_one",
                    "bonus_source": "spell_attack",
                }
            ],
            [
                {
                    "id": "damage",
                    "kind": "damage",
                    "target_mode": "selected_one",
                    "dice": "3d10",
                    "damage_type": "necrotic",
                    "when": {
                        "check_template_id": "spell_hit",
                        "outcomes": ["hit", "critical"],
                    },
                }
            ],
        )

    save_ability = "dexterity" if skill_id == "sacred_flame" else "constitution"
    dice = (
        f"{1 + (actor.level >= 5) + (actor.level >= 11) + (actor.level >= 17)}d8"
        if skill_id == "sacred_flame"
        else "14d6"
    )
    damage_type = "radiant" if skill_id == "sacred_flame" else "necrotic"
    checks = [
        {
            "id": "saving_throw",
            "kind": "saving_throw",
            "roller": "target",
            "target_mode": "selected_one",
            "ability": save_ability,
            "dc_source": "spell_save",
        }
    ]
    effects = [
        {
            "id": "damage",
            "kind": "damage",
            "target_mode": "selected_one",
            "dice": dice,
            "damage_type": damage_type,
            "when": {
                "check_template_id": "saving_throw",
                "outcomes": ["failure"],
            },
        }
    ]
    if skill_id == "harm":
        effects.append(
            {
                **effects[0],
                "id": "damage_on_save",
                "multiplier": 0.5,
                "when": {
                    "check_template_id": "saving_throw",
                    "outcomes": ["success"],
                },
            }
        )
    return enemy_one, checks, effects


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
    definitions.append((HEALING_POTION, None))
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
    definitions.append((HEALING_POTION, None))
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
        legal_targets = _legal_targets(definition, actor, targets, scope=scope)
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
    if not actor.is_alive:
        return "角色已倒下，无法行动"
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
            item.item_id == item_id
            and item.quantity >= int(definition.usage.get("quantity", 1))
            for item in actor.inventory
        ):
            return "背包物品数量不足"
    return None


def _legal_targets(
    definition: ActionDefinition,
    actor: Combatant,
    targets: dict[str, Combatant],
    *,
    scope: str = "combat",
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
        if (
            scope == "combat"
            and distance == "melee"
            and not in_reach(actor, target, False)
        ):
            continue
        result.append(target)
    return result
