"""中断交互协议（骰子交给玩家）。

实现 docs/战斗/03-中断交互协议.md：构造「图 → 前端」的中断请求负载，
以及构造「声明行动」节点要推给玩家的合法选项。恢复值由前端按文档格式回报，
各节点自行读取 `Command(resume=...)` 的字典，本模块只负责出请求与做范围校验。
"""

from __future__ import annotations

from typing import Any

from src.combat.rules import in_reach
from src.character.skills import is_combat_skill, skill_definition
from src.model.combatant import Character, Combatant
from src.model.enums import InterruptType


def build_interrupt_request(
    *,
    kind: InterruptType,
    actor: Combatant,
    prompt: str,
    required_dice: str | None = None,
    bonus: int = 0,
    options: dict | None = None,
    extra: dict | None = None,
    expected_return: dict | None = None,
) -> dict[str, Any]:
    """统一的中断请求负载（见文档第 2 节）。

    `directed_to.user_id` = 操控者，前端据此把「该谁掷什么」推给正确的人。
    """
    request: dict[str, Any] = {
        "interrupt_type": str(kind.value),  # 中断类型
        "directed_to": {  # 面向：推给谁
            "combatant_id": actor.id,
            "user_id": actor.controller,
        },
        "prompt": prompt,  # 提示
        "required_dice": required_dice,  # 需要骰子
        "bonus": bonus,  # 加值（引擎会替你加的固定值）
        "options": options,  # 选项（仅声明行动用）
        "expected_return": expected_return
        or _default_expected_return(kind),  # 期望返回
    }
    if extra:
        request["extra"] = extra  # 附带
    return request


def _default_expected_return(kind: InterruptType) -> dict:
    """各中断类型的恢复值 schema 提示（文档第 3 节）。"""
    if kind == InterruptType.DAMAGE_ROLL:
        return {"result": "int 伤害总和"}
    if kind == InterruptType.DECLARE_ACTION:
        return {"action_type": "str", "target_id": "str(可选)"}
    # 掷先攻 / 攻击检定 / 豁免检定 / 属性检定
    return {"d20": "int 1-20 原始值"}


def build_action_options(
    actor: Combatant,
    combatants: dict[str, Combatant],
    *,
    special_actions: list[dict[str, Any]] | None = None,
    story_flags: list[str] | None = None,
    applied_special_actions: list[str] | None = None,
    actions_remaining: int = 1,
    extra_attacks_remaining: int = 0,
    attack_action_started: bool = False,
) -> dict[str, Any]:
    """为「声明行动」中断构造合法选项（文档 2.1）。

    - 攻击：每件武器列出射程内、存活的敌方目标（按区域过滤）。
    - 自然语言：只能由真实 DM 映射为这里列出的封闭行动。
    - 特殊行动：还要满足线索、道具、目标与射程条件，且每场战斗只成功一次。
    """
    enemies_alive = [
        c for c in combatants.values() if c.faction != actor.faction and c.is_alive
    ]
    has_general_action = int(actions_remaining) > 0
    has_extra_attack = attack_action_started and int(extra_attacks_remaining) > 0

    attack_options = []
    for weapon in actor.attacks:
        targets = [
            {"id": t.id, "name": t.name, "zone": t.current_zone}
            for t in enemies_alive
            if in_reach(actor, t, weapon.is_ranged)
        ]
        attack_options.append(
            {
                "attack_name": weapon.name,
                "range": str(weapon.attack_range.value),
                "targets": targets,
            }
        )

    options: dict[str, Any] = {
        "attack": attack_options,
        "natural_language": True,
        "pass": True,
    }

    # 移动：可去的其他区域
    all_zones = sorted({c.current_zone for c in combatants.values()})
    options["move"] = (
        [{"target_zone": zone} for zone in all_zones if zone != actor.current_zone]
        if has_general_action
        else []
    )

    if isinstance(actor, Character) and has_general_action:
        target_options = [
            {
                "id": target.id,
                "name": target.name,
                "faction": target.faction.value,
                "zone": target.current_zone,
                "life_state": target.life_state.value,
            }
            for target in combatants.values()
        ]
        skill_options: list[dict[str, Any]] = []
        for skill in actor.skills:
            if not skill.is_available or not is_combat_skill(skill.skill_id):
                continue
            definition = skill_definition(skill.skill_id) or {}
            targets = target_options
            if definition.get("target_scope") == "same_zone_enemy_alive":
                legal_ids = {
                    target.id
                    for target in combatants.values()
                    if target.faction != actor.faction
                    and target.is_alive
                    and target.current_zone == actor.current_zone
                }
                targets = [
                    target for target in target_options if target["id"] in legal_ids
                ]
            min_targets = int(definition.get("min_targets", 1))
            if len(targets) < min_targets:
                continue
            skill_options.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name or skill.skill_id,
                    "source_type": skill.source_type,
                    "types": list(skill.types),
                    "charges_left": skill.charges,
                    "cooldown_left": max(0, skill.cooldown_left - 1),
                    "min_targets": min_targets,
                    "max_targets": int(definition.get("max_targets", 20)),
                    "targets": targets,
                }
            )
        options["skill"] = skill_options
        options["item"] = [
            {"item_id": i.item_id, "quantity": i.quantity}
            for i in actor.inventory
            if i.is_available
        ]
    options["special"] = (
        _available_special_actions(
            actor,
            combatants,
            special_actions=special_actions or [],
            story_flags=set(story_flags or []),
            applied_special_actions=set(applied_special_actions or []),
        )
        if has_general_action
        else []
    )
    options["actions_remaining"] = max(0, int(actions_remaining)) + max(
        0, int(extra_attacks_remaining)
    )
    options["general_actions_remaining"] = max(0, int(actions_remaining))
    options["extra_attacks_remaining"] = max(0, int(extra_attacks_remaining))
    options["attack_only"] = not has_general_action and has_extra_attack
    return options


def _available_special_actions(
    actor: Combatant,
    combatants: dict[str, Combatant],
    *,
    special_actions: list[dict[str, Any]],
    story_flags: set[str],
    applied_special_actions: set[str],
) -> list[dict[str, Any]]:
    """筛出当前角色此刻确实可以声明的 canon 特殊行动。"""
    available: list[dict[str, Any]] = []
    for definition in special_actions:
        action_id = str(definition.get("id", ""))
        if not action_id or action_id in applied_special_actions:
            continue
        required_flags = set(definition.get("requires_flags", []) or [])
        if not required_flags.issubset(story_flags):
            continue
        required_item_id = definition.get("requires_item_id")
        if required_item_id and not _has_item(actor, str(required_item_id)):
            continue

        target = combatants.get(str(definition.get("target_actor_id", "")))
        if not target or not target.is_alive or target.faction == actor.faction:
            continue
        if (
            definition.get("range") == "melee"
            and target.current_zone != actor.current_zone
        ):
            continue

        check = definition.get("check")
        public_action: dict[str, Any] = {
            "special_action_id": action_id,
            "label": definition.get("label", action_id),
            "description": definition.get("description", ""),
            "target_id": target.id,
            "target_name": target.name,
        }
        if isinstance(check, dict):
            public_action["check"] = {
                "ability": check.get("ability"),
                "dc": int(check.get("dc", 10)),
            }
        available.append(public_action)
    return available


def _has_item(actor: Combatant, item_id: str) -> bool:
    """判断角色背包中是否仍有指定道具。"""
    if not isinstance(actor, Character):
        return False
    return any(
        item.item_id == item_id and item.is_available for item in actor.inventory
    )


def validate_d20(resume_value: Any, *, default: int = 10) -> int:
    """从恢复值里取 d20 原始值并做 1–20 范围校验（信任边界：加值一律引擎算）。"""
    if isinstance(resume_value, dict):
        raw = resume_value.get("d20", default)
    else:
        raw = resume_value
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(20, value))


def extract_damage(resume_value: Any) -> int | None:
    """从恢复值里取玩家自报的伤害总和（攻击检定可一次带回 `damage_result`）。"""
    if not isinstance(resume_value, dict):
        return None
    for key in ("damage_result", "result"):
        if key in resume_value:
            try:
                return max(0, int(resume_value[key]))
            except (TypeError, ValueError):
                return None
    return None


def extract_roll_source(resume_value: Any, *, default: str = "manual") -> str:
    """读取骰子来源，仅接受公开协议中的 ``manual`` / ``virtual``。"""
    if not isinstance(resume_value, dict):
        return default
    source = resume_value.get("source", default)
    return source if source in {"manual", "virtual"} else default


def build_combat_view(state: dict, *, actor_id: str | None = None) -> dict[str, Any]:
    """从战斗状态构造可公开给玩家的紧凑战况摘要。"""
    combatants = state.get("combatants", {}) or {}
    order = list(state.get("initiative_order", []) or [])
    current_index = int(state.get("current_index", -1))
    current_actor_id = actor_id
    if 0 <= current_index < len(order):
        current_actor_id = order[current_index]

    return {
        "round": int(state.get("current_round", 0)),
        "current_actor_id": current_actor_id,
        "initiative_order": order,
        "recent_events": list(state.get("combat_log", []) or [])[-6:],
        "feed": _build_combat_feed(list(state.get("combat_log", []) or [])),
        "combatants": [
            {
                "id": combatant.id,
                "name": combatant.name,
                "faction": str(combatant.faction.value),
                "current_hp": combatant.current_hp,
                "max_hp": combatant.max_hp,
                "temporary_hp": combatant.temporary_hp,
                "ac": combatant.ac,
                "life_state": str(combatant.life_state.value),
                "current_zone": combatant.current_zone,
                "initiative": combatant.initiative,
                "conditions": [
                    str(condition.kind.value) for condition in combatant.conditions
                ],
            }
            for combatant in combatants.values()
        ],
    }


def _build_combat_feed(combat_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把战斗事件压成只含玩家宣言与 DM 叙述的公开消息流。"""
    feed: list[dict[str, Any]] = []
    for index, event in enumerate(combat_log):
        event_type = event.get("event")
        if event_type == "declaration" and event.get("text"):
            feed.append(
                {
                    "id": f"combat-{index}",
                    "role": "player",
                    "character_id": event.get("actor_id"),
                    "content": str(event["text"]),
                }
            )
        elif event_type in {"combat_opening", "narration"} and event.get("text"):
            feed.append(
                {
                    "id": f"combat-{index}",
                    "role": "dm",
                    "content": str(event["text"]),
                }
            )
    return feed
