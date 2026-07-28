"""统一行动计划的确定性资源提交与效果执行器。"""

from __future__ import annotations

from math import floor
from typing import Any

from langgraph.types import interrupt

from src.combat.dice import current_engine_dice
from src.combat.interrupts import (
    build_combat_view,
    build_interrupt_request,
    extract_damage,
    extract_roll_source,
    validate_d20,
)
from src.combat.rules import (
    ability_check_bonus,
    check_success,
    resolve_attack,
    saving_throw_bonus,
)
from src.model.combatant import Character, Combatant
from src.model.effects import Condition, InventoryItem
from src.model.enums import Ability, ConditionType, DamageType, InterruptType, LifeState


def commit_action_cost(
    actor: Combatant,
    plan: dict[str, Any],
    used_action_ids: list[str],
    committed_plan_ids: list[str],
) -> tuple[list[str], list[str], dict[str, Any]]:
    """在执行前原子提交技能/物品/次数成本。

    本函数不含中断；图将其放在独立节点中，后续骰子恢复不会重复扣除。
    """
    definition_id = str(plan["definition_id"])
    plan_id = str(plan.get("plan_id") or "")
    if not plan_id:
        raise ValueError("规则行动计划缺少 plan_id")
    if plan_id in committed_plan_ids:
        return (
            used_action_ids,
            committed_plan_ids,
            {
                "event": "action_commit_replayed",
                "actor": actor.id,
                "definition_id": definition_id,
                "plan_id": plan_id,
            },
        )
    source_kind = str(plan["source_kind"])
    source_ref = str(plan["source_ref"])
    usage = dict(plan.get("usage") or {"kind": "unlimited"})
    usage_kind = str(usage.get("kind") or "unlimited")
    event: dict[str, Any] = {
        "event": "action_committed",
        "actor": actor.id,
        "definition_id": definition_id,
        "source_kind": source_kind,
        "plan_id": plan_id,
    }
    if usage_kind == "skill_resource":
        if not isinstance(actor, Character):
            raise ValueError("只有角色可以提交技能成本")
        learned = next(
            (item for item in actor.skills if item.skill_id == source_ref), None
        )
        if learned is None or not learned.is_available:
            raise ValueError(f"技能 «{source_ref}» 在提交时已不可用")
        learned.consume()
        event["skill_id"] = source_ref
    elif usage_kind == "consume_item":
        item_id = str(usage.get("item_id") or source_ref)
        if not isinstance(actor, Character):
            raise ValueError("只有角色可以消耗物品")
        item = next(
            (entry for entry in actor.inventory if entry.item_id == item_id), None
        )
        if item is None or not item.is_available:
            raise ValueError(f"物品 «{item_id}» 在提交时已不存在")
        quantity = max(1, int(usage.get("quantity", 1)))
        if item.quantity < quantity:
            raise ValueError(f"物品 «{item_id}» 数量不足")
        item.quantity -= quantity
        event.update({"item_id": item_id, "quantity_left": item.quantity})
    if usage_kind in {"once_per_combat", "once_per_session"}:
        if definition_id in used_action_ids:
            raise ValueError(f"行动 «{definition_id}» 已使用")
        used_action_ids = [*used_action_ids, definition_id]
    return used_action_ids, [*committed_plan_ids, plan_id], event


def execute_combat_plan(
    state: dict[str, Any], actor: Combatant, plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """执行已校验的战斗计划；所有骰值与状态变化均由引擎掌握。"""
    combatants: dict[str, Combatant] = state["combatants"]
    results, check_events = _resolve_checks(
        plan, actor, combatants, state=state, scope="combat"
    )
    events: list[dict[str, Any]] = [
        {
            "event": "rule_action",
            "actor": actor.id,
            "definition_id": plan["definition_id"],
            "source_kind": plan["source_kind"],
            "source_ref": plan["source_ref"],
            "summary": plan.get("summary", ""),
        },
        *check_events,
    ]
    if plan.get("concentration") and isinstance(actor, Character):
        actor.concentration_skill_id = str(plan["source_ref"])
    for effect in plan.get("effects", []):
        if not _branch_matches(effect, results):
            continue
        effect = dict(effect)
        effect["critical"] = (
            results.get(str((effect.get("when") or {}).get("check_id") or ""))
            == "critical"
        )
        target = combatants.get(str(effect.get("target_id") or ""))
        if target is None:
            raise ValueError("战斗效果目标在执行时不存在")
        events.extend(_apply_character_effect(state, actor, target, effect, plan))
    return events


def execute_world_plan(
    state: dict[str, Any], actor: Combatant, plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行世界计划并返回受限世界写入，供故事节点统一原子提交。"""
    party: dict[str, Combatant] = state.get("party") or {}
    results, check_events = _resolve_checks(
        plan, actor, party, state=state, scope="world"
    )
    events: list[dict[str, Any]] = [
        {
            "event": "rule_action",
            "actor": actor.id,
            "definition_id": plan["definition_id"],
            "source_kind": plan["source_kind"],
            "source_ref": plan["source_ref"],
            "summary": plan.get("summary", ""),
        },
        *check_events,
    ]
    writes: dict[str, Any] = {}
    active_effects = [
        effect for effect in plan.get("effects", []) if _branch_matches(effect, results)
    ]
    preflight_world_effects(active_effects, actor, party)
    for effect in active_effects:
        kind = str(effect["kind"])
        if kind in {
            "damage",
            "healing",
            "temporary_hp",
            "add_condition",
            "remove_condition",
            "revive",
        }:
            target = party.get(str(effect.get("target_id") or ""))
            if target is None:
                raise ValueError("世界角色效果目标不存在")
            events.extend(_apply_character_effect(state, actor, target, effect, plan))
            continue
        if kind == "set_flag":
            writes.setdefault("flags_set", {})[str(effect["flag"])] = effect.get(
                "value", True
            )
        elif kind == "grant_item":
            target = party.get(str(effect.get("target_id") or actor.id))
            if not isinstance(target, Character):
                raise ValueError("物品发放目标不是角色")
            quantity = max(1, int(effect.get("quantity", 1)))
            item_id = str(effect["item_id"])
            owned = next(
                (item for item in target.inventory if item.item_id == item_id), None
            )
            if owned is None:
                target.inventory.append(
                    InventoryItem(item_id=item_id, quantity=quantity)
                )
            else:
                owned.quantity += quantity
            events.append(
                {
                    "event": "item_granted",
                    "actor": target.id,
                    "item_id": item_id,
                    "quantity": quantity,
                }
            )
        elif kind == "remove_item":
            target = party.get(str(effect.get("target_id") or actor.id))
            if not isinstance(target, Character):
                raise ValueError("物品移除目标不是角色")
            quantity = max(1, int(effect.get("quantity", 1)))
            item_id = str(effect["item_id"])
            owned = next(
                (item for item in target.inventory if item.item_id == item_id), None
            )
            if owned is None or owned.quantity < quantity:
                raise ValueError(f"物品 «{item_id}» 数量不足")
            owned.quantity -= quantity
            events.append(
                {
                    "event": "item_removed",
                    "actor": target.id,
                    "item_id": item_id,
                    "quantity": quantity,
                }
            )
        elif kind == "discover_clue":
            writes.setdefault("discoveries", []).append(str(effect["clue_id"]))
        elif kind == "move_location":
            writes["moved_to"] = str(effect["location_id"])
        elif kind == "transition_beat":
            writes["transition_to_beat_id"] = str(effect["beat_id"])
        else:
            raise ValueError(f"世界执行器不支持效果 «{kind}»")
        events.append(
            {
                "event": kind,
                "actor": actor.id,
                **{key: value for key, value in effect.items() if key not in {"when"}},
            }
        )
    return events, writes


def preflight_world_effects(
    effects: list[dict[str, Any]],
    actor: Combatant,
    party: dict[str, Combatant],
) -> None:
    """在产生任何世界变更前校验目标与累计物品移除成本。"""
    removals: dict[tuple[str, str], int] = {}
    character_kinds = {
        "damage",
        "healing",
        "temporary_hp",
        "add_condition",
        "remove_condition",
        "revive",
    }
    for effect in effects:
        kind = str(effect["kind"])
        if kind in character_kinds:
            if party.get(str(effect.get("target_id") or "")) is None:
                raise ValueError("世界角色效果目标不存在")
            continue
        if kind not in {"grant_item", "remove_item"}:
            continue
        target_id = str(effect.get("target_id") or actor.id)
        target = party.get(target_id)
        if not isinstance(target, Character):
            raise ValueError("物品变更目标不是角色")
        if kind == "remove_item":
            item_id = str(effect["item_id"])
            key = (target_id, item_id)
            removals[key] = removals.get(key, 0) + max(
                1, int(effect.get("quantity", 1))
            )
    for (target_id, item_id), quantity in removals.items():
        target = party[target_id]
        owned = next(
            (
                item
                for item in getattr(target, "inventory", [])
                if item.item_id == item_id
            ),
            None,
        )
        if owned is None or owned.quantity < quantity:
            raise ValueError(f"物品 «{item_id}» 数量不足")


def _resolve_checks(
    plan: dict[str, Any],
    actor: Combatant,
    combatants: dict[str, Combatant],
    *,
    state: dict[str, Any],
    scope: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    results: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for check in plan.get("checks", []):
        kind = str(check["kind"])
        roller = combatants.get(str(check.get("roller_id") or ""))
        target = combatants.get(str(check.get("target_id") or ""))
        if roller is None:
            raise ValueError("行动检定的掷骰者不存在")
        bonus = _check_bonus(check, roller, actor)
        d20, source = _roll_d20(
            state,
            roller,
            check,
            bonus=bonus,
            scope=scope,
            action_id=str(plan["definition_id"]),
        )
        if kind == "attack_roll":
            if target is None:
                raise ValueError("攻击检定缺少目标")
            result = resolve_attack(d20, bonus, target.ac)
            outcome = "critical" if result.crit else "hit" if result.hit else "miss"
        else:
            dc = _check_dc(check, actor)
            outcome = "success" if check_success(d20, bonus, dc) else "failure"
        results[str(check["id"])] = outcome
        events.append(
            {
                "event": "action_check",
                "action_id": plan["definition_id"],
                "check_id": check["id"],
                "kind": kind,
                "roller": roller.id,
                "target": target.id if target else None,
                "d20": d20,
                "bonus": bonus,
                "dc": (
                    _check_dc(check, actor)
                    if kind != "attack_roll"
                    else (target.ac if target else None)
                ),
                "source": source,
                "outcome": outcome,
            }
        )
    return results, events


def _roll_d20(
    state: dict[str, Any],
    roller: Combatant,
    check: dict[str, Any],
    *,
    bonus: int,
    scope: str,
    action_id: str,
) -> tuple[int, str]:
    if roller.is_player_controlled:
        kind = {
            "attack_roll": InterruptType.ATTACK_ROLL,
            "saving_throw": InterruptType.SAVING_THROW,
            "ability_check": InterruptType.ABILITY_CHECK,
        }[str(check["kind"])]
        extra: dict[str, Any] = {
            "action_id": action_id,
            "check_id": check["id"],
            "purpose": "rule_action_check",
        }
        if scope == "combat":
            extra["combat"] = build_combat_view(state, actor_id=roller.id)
        resumed = interrupt(
            build_interrupt_request(
                kind=kind,
                actor=roller,
                prompt=f"为规则行动 «{action_id}» 进行 {check['kind']}：掷 d20 + {bonus}",
                required_dice="d20",
                bonus=bonus,
                extra=extra,
            )
        )
        return validate_d20(resumed), extract_roll_source(resumed)
    return current_engine_dice().d20(), "engine"


def _roll_amount(
    state: dict[str, Any],
    actor: Combatant,
    effect: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[int, str]:
    if effect.get("dice") is None:
        amount, source = int(effect.get("amount", 0)), "fixed"
    elif actor.is_player_controlled:
        extra: dict[str, Any] = {
            "action_id": plan["definition_id"],
            "effect_id": effect["id"],
            "purpose": "rule_action_effect",
            "crit": bool(effect.get("critical")),
        }
        if plan.get("scope") == "combat":
            extra["combat"] = build_combat_view(state, actor_id=actor.id)
        resumed = interrupt(
            build_interrupt_request(
                kind=InterruptType.EFFECT_ROLL,
                actor=actor,
                prompt=f"为「{plan.get('summary') or plan['definition_id']}」掷 {effect['dice']}",
                required_dice=str(effect["dice"]),
                extra=extra,
            )
        )
        amount = extract_damage(resumed)
        if amount is None:
            raise ValueError("效果骰中断缺少有效结果")
        source = extract_roll_source(resumed)
    else:
        amount = (
            current_engine_dice()
            .roll(str(effect["dice"]), crit=bool(effect.get("critical")))
            .total
        )
        source = "engine"
    if effect.get("amount_bonus_source") == "spellcasting_modifier" and isinstance(
        actor, Character
    ):
        amount += _spellcasting_modifier(actor)
    elif effect.get("amount_bonus_source") == "actor_level":
        amount += int(getattr(actor, "level", 1))
    amount = floor(amount * float(effect.get("multiplier", 1)))
    return max(0, amount), source


def _apply_character_effect(
    state: dict[str, Any],
    actor: Combatant,
    target: Combatant,
    effect: dict[str, Any],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    kind = str(effect["kind"])
    base = {"actor": actor.id, "target": target.id, "effect_id": effect["id"]}
    if kind == "damage":
        amount, source = _roll_amount(state, actor, effect, plan)
        dealt = target.take_damage(amount)
        events = [
            {
                "event": "action_damage",
                **base,
                "damage": dealt,
                "damage_type": effect["damage_type"],
                "source": source,
                "target_hp": target.current_hp,
            }
        ]
        combatants = state.get("combatants")
        if isinstance(combatants, dict):
            from src.combat.nodes import _check_concentration

            concentration = _check_concentration(state, target, dealt, combatants)
            if concentration:
                events.append(concentration)
        return events
    if kind == "healing":
        amount, source = _roll_amount(state, actor, effect, plan)
        healed = target.heal(amount)
        return [
            {
                "event": "action_healing",
                **base,
                "healing": healed,
                "source": source,
                "target_hp": target.current_hp,
            }
        ]
    if kind == "temporary_hp":
        amount, source = _roll_amount(state, actor, effect, plan)
        target.temporary_hp = max(target.temporary_hp, amount)
        return [{"event": "temporary_hp", **base, "amount": amount, "source": source}]
    if kind == "revive":
        amount, source = _roll_amount(state, actor, effect, plan)
        target.life_state = LifeState.ALIVE
        target.current_hp = min(target.max_hp, max(1, amount))
        return [
            {
                "event": "revive",
                **base,
                "source": source,
                "target_hp": target.current_hp,
            }
        ]
    if kind == "add_condition":
        condition = Condition(
            kind=ConditionType(str(effect["condition"])),
            rounds_left=max(1, int(effect.get("rounds", 1))),
            amount=int(effect.get("amount", 0)),
            damage_type=(
                DamageType(str(effect["damage_type"]))
                if effect.get("damage_type")
                else None
            ),
            source_skill_id=str(plan["source_ref"]),
            source_actor_id=actor.id,
        )
        target.add_condition(condition)
        return [
            {
                "event": "condition_added",
                **base,
                "condition": condition.kind.value,
                "rounds": condition.rounds_left,
            }
        ]
    if kind == "remove_condition":
        condition_name = effect.get("condition")
        before = len(target.conditions)
        removed = [
            item
            for item in target.conditions
            if condition_name is None or item.kind.value == condition_name
        ]
        for item in removed:
            if item.stat == "ac":
                target.ac -= item.amount
            if item.stat == "attack_bonus":
                for attack in target.attacks:
                    attack.attack_bonus -= item.amount
        target.conditions = [item for item in target.conditions if item not in removed]
        return [
            {
                "event": "condition_removed",
                **base,
                "removed": before - len(target.conditions),
            }
        ]
    if kind in {"modify_ac", "modify_attack_bonus"}:
        stat = "ac" if kind == "modify_ac" else "attack_bonus"
        condition = Condition(
            kind=(
                ConditionType.BUFF
                if int(effect["amount"]) >= 0
                else ConditionType.DEBUFF
            ),
            rounds_left=max(1, int(effect.get("rounds", 1))),
            amount=int(effect["amount"]),
            stat=stat,
            source_skill_id=str(plan["source_ref"]),
            source_actor_id=actor.id,
        )
        target.add_condition(condition)
        return [
            {
                "event": kind,
                **base,
                "amount": condition.amount,
                "rounds": condition.rounds_left,
            }
        ]
    if kind == "move_zone":
        old_zone = target.current_zone
        target.current_zone = str(effect["target_zone"])
        return [
            {"event": "move_zone", **base, "from": old_zone, "to": target.current_zone}
        ]
    raise ValueError(f"角色效果执行器不支持 «{kind}»")


def _branch_matches(effect: dict[str, Any], results: dict[str, str]) -> bool:
    when = effect.get("when") or {"outcomes": ["always"]}
    outcomes = set(str(value) for value in when.get("outcomes", ["always"]))
    if "always" in outcomes:
        return True
    return results.get(str(when.get("check_id") or "")) in outcomes


def _check_bonus(check: dict[str, Any], roller: Combatant, actor: Combatant) -> int:
    source = str(check.get("bonus_source") or "ability")
    if source == "weapon_attack":
        return actor.attacks[0].attack_bonus if actor.attacks else 0
    if source == "spell_attack" and isinstance(actor, Character):
        return actor.proficiency_bonus + _spellcasting_modifier(actor)
    ability = Ability(str(check.get("ability") or Ability.DEXTERITY.value))
    if str(check["kind"]) == "saving_throw":
        return saving_throw_bonus(roller, ability)
    return ability_check_bonus(
        roller, ability, proficient=bool(check.get("proficient"))
    )


def _check_dc(check: dict[str, Any], actor: Combatant) -> int:
    if check.get("fixed_dc") is not None:
        return int(check["fixed_dc"])
    if check.get("dc_source") == "spell_save" and isinstance(actor, Character):
        return 8 + actor.proficiency_bonus + _spellcasting_modifier(actor)
    raise ValueError("非攻击检定缺少受支持的 DC 来源")


def _spellcasting_modifier(actor: Character) -> int:
    ability = {
        "bard": Ability.CHARISMA,
        "cleric": Ability.WISDOM,
        "paladin": Ability.CHARISMA,
    }.get(actor.class_id, Ability.INTELLIGENCE)
    return actor.modifier(ability)
