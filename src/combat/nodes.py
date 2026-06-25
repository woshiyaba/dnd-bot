"""战斗子图的节点实现。

每个节点读写 docs/战斗/01 定义的 `CombatState`，对照 docs/战斗/02 的流程：

    enter_combat → judge_surprise → roll_initiative → next_turn
    → declare_action → resolve_action → narrate → check_end ─┐
              ▲────────────────(outcome==进行中)─────────────┘
              └──(否则)──► settle → END

设计原则：**规则归引擎，叙述归 DM，骰子归玩家**。
- 引擎节点 = 纯 Python 确定性结算；
- 玩家骰子 = `interrupt()` 收集（仅 `is_player_controlled` 的参战者）；
- 怪物/环境骰子 = 引擎用可复现随机源自动掷；
- DM 决策/叙述目前为确定性启发式占位（见 `_dm_decide` / `narrate`），保留接 LLM 的钩子。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from src.combat.dice import Dice
from src.combat.interrupts import (
    build_action_options,
    build_interrupt_request,
    extract_damage,
    validate_d20,
)
from src.combat.rules import (
    ability_check_bonus,
    check_success,
    in_reach,
    resolve_attack,
)
from src.model.combat_state import CombatState, load_combatants
from src.model.combatant import Character, Combatant
from src.model.enums import (
    Ability,
    ActionType,
    CombatOutcome,
    CombatPhase,
    ConditionType,
    Faction,
    InterruptType,
)

logger = logging.getLogger(__name__)

# 可复现随机源：怪物/环境骰子走这里。可在 enter_combat 用场景里的「random_seed」重置。
_dice = Dice()


def _reset_dice(seed: int | None) -> None:
    """用场景随机种子重置引擎骰子，保证回放/测试可复现。"""
    global _dice
    _dice = Dice(seed)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _current_actor(state: CombatState) -> Combatant:
    """取先攻指针指向的参战者。"""
    actor_id = state["initiative_order"][state["current_index"]]
    return state["combatants"][actor_id]


def _append_log(state: CombatState, events: list[dict]) -> list[dict]:
    """把本回合事件追加进全场日志，返回新的日志列表。"""
    log = list(state.get("combat_log", []))
    log.extend(events)
    return log


def _with_round(state: CombatState, event: dict) -> dict:
    """给事件补上轮次信息，便于前端回放与 DM 上下文。"""
    event.setdefault("round", state.get("current_round"))
    return event


# ---------------------------------------------------------------------------
# 1. enter_combat（引擎）
# ---------------------------------------------------------------------------
def enter_combat(state: CombatState) -> dict:
    """初始化战斗：加载参战者、摆好区域、清空工作区。"""
    scene = state.get("scene_context", {}) or {}
    if "random_seed" in scene:
        _reset_dice(int(scene["random_seed"]))

    combatants = state.get("combatants") or load_combatants(scene)

    logger.info("[enter_combat] 进入战斗 | 参战者=%d", len(combatants))
    return {
        "combatants": combatants,
        "initiative_order": [],
        "current_index": -1,
        "current_round": 0,
        "phase": CombatPhase.SETUP,
        "outcome": CombatOutcome.ONGOING,
        "current_action": None,
        "turn_events": [],
        "combat_log": list(state.get("combat_log", [])),
    }


# ---------------------------------------------------------------------------
# 2. judge_surprise（DM）
# ---------------------------------------------------------------------------
def judge_surprise(state: CombatState) -> dict:
    """判定突袭。

    v0 简化：纯叙事判定，不掷隐匿/察觉。被突袭名单由 `scene_context["surprised"]`（id 列表）给出，
    缺省则无人被突袭。日后接 DM（LLM）时在此节点替换为「潜行 vs 被动察觉」对抗。
    """
    scene = state.get("scene_context", {}) or {}
    combatants = state["combatants"]
    surprised = [cid for cid in scene.get("surprised", []) if cid in combatants]
    for cid in surprised:
        combatants[cid].is_surprised = True

    events = [{"event": "surprise_check", "surprised": surprised}]
    logger.info("[judge_surprise] 被突袭=%s", surprised)
    return {
        "combatants": combatants,
        "phase": CombatPhase.SURPRISE,
        "combat_log": _append_log(state, events),
    }


# ---------------------------------------------------------------------------
# 3. roll_initiative（引擎 + 玩家中断）
# ---------------------------------------------------------------------------
def roll_initiative(state: CombatState) -> dict:
    """掷先攻、排定行动顺序。

    玩家参战者逐个 `interrupt` 报 d20（引擎加先攻调整值）；怪物引擎自动掷。
    多个玩家时按字典顺序逐个中断收集（同一时刻只挂起一个）。
    """
    combatants = state["combatants"]

    for c in combatants.values():
        if c.is_player_controlled:
            prompt = f"轮到 {c.name}，掷先攻：d20 + {c.effective_initiative_bonus}"
            resume_value = interrupt(build_interrupt_request(
                kind=InterruptType.ROLL_INITIATIVE,
                actor=c,
                prompt=prompt,
                required_dice="d20",
                bonus=c.effective_initiative_bonus,
            ))
            d20 = validate_d20(resume_value)
        else:
            d20 = _dice.d20()
        c.initiative = d20 + c.effective_initiative_bonus

    # 降序排序；平手用敏捷调整值，再用引擎随机数打破
    order = sorted(
        combatants.values(),
        key=lambda c: (c.initiative, c.modifier(Ability.DEXTERITY), _dice.d20()),
        reverse=True,
    )
    initiative_order = [c.id for c in order]

    events = [{"event": "roll_initiative", "initiative_order": [
        {"id": c.id, "name": c.name, "initiative": c.initiative} for c in order
    ]}]
    logger.info("[roll_initiative] 先攻顺序=%s", initiative_order)
    return {
        "combatants": combatants,
        "initiative_order": initiative_order,
        "current_index": -1,
        "current_round": 1,
        "phase": CombatPhase.INITIATIVE,
        "combat_log": _append_log(state, events),
    }


# ---------------------------------------------------------------------------
# 4. next_turn（引擎）
# ---------------------------------------------------------------------------
def next_turn(state: CombatState) -> dict:
    """推进先攻指针，处理回合开始结算与跳过。

    指针在本节点入口推进（保证图里只有一条回边）。回合开始：结算持续伤害、
    递减状态；若行动者倒下 / 被突袭(首轮) / 眩晕，则跳过并继续推进。
    """
    combatants = state["combatants"]
    order = state["initiative_order"]
    index = state["current_index"]
    rnd = state["current_round"]
    events: list[dict] = []

    safety = 0
    while True:
        safety += 1
        if safety > len(order) * 4 + 8:
            # 兜底：理论上不会触发（check_end 已保证两阵营都还有活人）
            break

        index += 1
        if index >= len(order):
            index = 0
            rnd += 1

        actor = combatants[order[index]]

        if not actor.is_alive:
            continue  # 倒下者直接跳过，不结算

        # —— 回合开始结算：持续伤害 ——
        for s in list(actor.conditions):
            if s.kind == ConditionType.DAMAGE_OVER_TIME and s.amount > 0:
                dealt = actor.take_damage(s.amount)
                events.append(_with_round(state | {"current_round": rnd}, {
                    "event": "damage_over_time", "actor": actor.id,
                    "damage": dealt, "current_hp": actor.current_hp,
                }))

        was_stunned = actor.has_condition(ConditionType.STUNNED)
        actor.tick_conditions()

        if not actor.is_alive:
            events.append({"event": "down", "actor": actor.id, "reason": "damage_over_time", "round": rnd})
            continue
        if actor.is_surprised and rnd == 1:
            events.append({"event": "skip", "actor": actor.id, "reason": "surprised", "round": rnd})
            continue
        if was_stunned:
            events.append({"event": "skip", "actor": actor.id, "reason": "stunned", "round": rnd})
            continue
        break

    logger.info("[next_turn] 轮次=%d 指针=%d 行动者=%s", rnd, index, order[index])
    return {
        "combatants": combatants,
        "current_index": index,
        "current_round": rnd,
        "phase": CombatPhase.IN_TURN,
        "current_action": None,
        "turn_events": [],
        "combat_log": _append_log(state, events),
    }


# ---------------------------------------------------------------------------
# 5. declare_action（玩家中断 / DM）
# ---------------------------------------------------------------------------
def declare_action(state: CombatState) -> dict:
    """声明行动与目标：玩家中断选择；怪物/NPC 由 DM 决策（v0 启发式）。"""
    combatants = state["combatants"]
    actor = _current_actor(state)

    if actor.is_player_controlled:
        options = build_action_options(actor, combatants)
        resume_value = interrupt(build_interrupt_request(
            kind=InterruptType.DECLARE_ACTION,
            actor=actor,
            prompt=f"轮到 {actor.name}，声明你的行动",
            options=options,
        ))
        current_action = _normalize_action(resume_value, actor, combatants)
    else:
        current_action = _dm_decide(actor, combatants)

    logger.info("[declare_action] %s -> %s", actor.id, current_action)
    return {"current_action": current_action}


def _normalize_action(resume_value: Any, actor: Combatant, combatants: dict[str, Combatant]) -> dict:
    """把玩家回报的恢复值规范成统一的「current_action」结构。"""
    if not isinstance(resume_value, dict):
        return {"action_type": ActionType.PASS.value}
    action = dict(resume_value)
    action.setdefault("action_type", ActionType.PASS.value)
    return action


def _dm_decide(actor: Combatant, combatants: dict[str, Combatant]) -> dict:
    """怪物/NPC 的确定性决策（占位，可替换为 LLM）。

    策略：选第一件能够得着存活敌人的武器，打血量最低的目标；
    都够不着 → 移动到最近敌人的区域；没有敌人 → 放弃。
    """
    enemies_alive = [c for c in combatants.values() if c.faction != actor.faction and c.is_alive]
    if not enemies_alive:
        return {"action_type": ActionType.PASS.value}

    for weapon in actor.attacks:
        reachable = [t for t in enemies_alive if in_reach(actor, t, weapon.is_ranged)]
        if reachable:
            target = min(reachable, key=lambda t: t.current_hp)
            return {
                "action_type": ActionType.ATTACK.value,
                "attack_name": weapon.name,
                "target_id": target.id,
            }

    # 够不着任何人：移动到最近敌人的区域（下回合再打）
    target = min(enemies_alive, key=lambda t: t.current_hp)
    return {"action_type": ActionType.MOVE.value, "target_zone": target.current_zone}


# ---------------------------------------------------------------------------
# 6. resolve_action（引擎 + 玩家中断）
# ---------------------------------------------------------------------------
def resolve_action(state: CombatState) -> dict:
    """按「current_action」类型做确定性结算，产出结构化事件。"""
    combatants = state["combatants"]
    actor = _current_actor(state)
    action = state.get("current_action") or {"action_type": ActionType.PASS.value}
    action_type = action.get("action_type")

    if action_type == ActionType.ATTACK.value:
        events = _resolve_attack(actor, action, combatants)
    elif action_type == ActionType.SKILL.value:
        events = _resolve_skill(actor, action, combatants)
    elif action_type == ActionType.ITEM.value:
        events = _resolve_item(actor, action, combatants)
    elif action_type == ActionType.IMPROVISE.value:
        events = _resolve_improvise(actor, action, combatants)
    elif action_type == ActionType.MOVE.value:
        events = _resolve_move(actor, action)
    else:
        events = [{"event": "pass", "actor": actor.id}]

    events = [_with_round(state, e) for e in events]
    logger.info("[resolve_action] %s 事件=%s", actor.id, [e.get("event") for e in events])
    return {
        "combatants": combatants,
        "turn_events": events,
        "combat_log": _append_log(state, events),
    }


def _resolve_attack(actor: Combatant, action: dict, combatants: dict[str, Combatant]) -> list[dict]:
    """攻击结算：掷命中 → 判定 → 掷伤害 → 扣 HP，必要时置倒下。"""
    weapon = next((a for a in actor.attacks if a.name == action.get("attack_name")), None)
    if weapon is None and actor.attacks:
        weapon = actor.attacks[0]
    target = combatants.get(action.get("target_id", ""))

    if weapon is None or target is None or not target.is_alive:
        return [{"event": "invalid_attack", "actor": actor.id, "target": action.get("target_id")}]
    if not in_reach(actor, target, weapon.is_ranged):
        return [{"event": "out_of_reach", "actor": actor.id, "target": target.id, "attack_name": weapon.name}]

    # —— 命中骰：玩家中断（可一并报伤害）；怪物引擎掷 ——
    player_damage: int | None = None
    if actor.is_player_controlled:
        resume_value = interrupt(build_interrupt_request(
            kind=InterruptType.ATTACK_ROLL,
            actor=actor,
            prompt=f"{actor.name} 用「{weapon.name}」攻击 {target.name}：掷 d20 + {weapon.attack_bonus}",
            required_dice="d20",
            bonus=weapon.attack_bonus,
            extra={"damage_dice": weapon.damage_dice},
        ))
        d20 = validate_d20(resume_value)
        player_damage = extract_damage(resume_value)
    else:
        d20 = _dice.d20()

    result = resolve_attack(d20, weapon.attack_bonus, target.ac)
    event: dict = {
        "event": "attack", "actor": actor.id, "target": target.id,
        "attack_name": weapon.name, "d20": d20, "hit": result.hit, "crit": result.crit,
    }

    if not result.hit:
        return [event]

    # —— 伤害骰 ——
    if result.crit:
        # 重击需翻倍骰数：玩家补一次伤害掷骰中断；怪物引擎翻倍掷
        if actor.is_player_controlled:
            resume_value = interrupt(build_interrupt_request(
                kind=InterruptType.DAMAGE_ROLL,
                actor=actor,
                prompt=f"重击！把 {weapon.damage_dice} 的骰子数翻倍掷，报伤害总和",
                required_dice=weapon.damage_dice,
            ))
            damage = extract_damage(resume_value) or _dice.roll(weapon.damage_dice, crit=True).total
        else:
            damage = _dice.roll(weapon.damage_dice, crit=True).total
    else:
        if actor.is_player_controlled:
            damage = player_damage if player_damage is not None else _dice.roll(weapon.damage_dice).total
        else:
            damage = _dice.roll(weapon.damage_dice).total

    dealt = target.take_damage(damage)
    event.update({
        "damage": dealt, "damage_type": str(weapon.damage_type.value),
        "target_hp": target.current_hp, "target_alive": target.is_alive,
    })
    return [event]


_HEALING_SKILLS = {"skill_second_wind": "1d10"}


def _resolve_skill(actor: Combatant, action: dict, combatants: dict[str, Combatant]) -> list[dict]:
    """技能结算（v0）：扣充能；已知治疗技能回血，其余仅记事交 DM 叙述。"""
    skill_id = action.get("skill_id", "")
    owned = None
    if isinstance(actor, Character):
        owned = next((s for s in actor.skills if s.skill_id == skill_id), None)
    if owned is None or not owned.is_available:
        return [{"event": "invalid_skill", "actor": actor.id, "skill_id": skill_id}]

    owned.charges -= 1
    event: dict = {"event": "skill", "actor": actor.id, "skill_id": skill_id}

    if skill_id in _HEALING_SKILLS:
        heal_amount = _dice.roll(_HEALING_SKILLS[skill_id]).total + getattr(actor, "level", 1)
        healed = actor.heal(heal_amount)
        event.update({"heal": healed, "target": actor.id, "target_hp": actor.current_hp})
    return [event]


_HEALING_ITEMS = {"item_healing_potion": "2d4+2"}


def _resolve_item(actor: Combatant, action: dict, combatants: dict[str, Combatant]) -> list[dict]:
    """道具结算（v0）：扣数量；已知治疗药水回血，其余仅记事。"""
    item_id = action.get("item_id", "")
    owned = None
    if isinstance(actor, Character):
        owned = next((i for i in actor.inventory if i.item_id == item_id), None)
    if owned is None or not owned.is_available:
        return [{"event": "invalid_item", "actor": actor.id, "item_id": item_id}]

    owned.quantity -= 1
    target = combatants.get(action.get("target_id", ""), actor)
    event: dict = {"event": "item", "actor": actor.id, "item_id": item_id, "target": target.id}

    if item_id in _HEALING_ITEMS:
        healed = target.heal(_dice.roll(_HEALING_ITEMS[item_id]).total)
        event.update({"heal": healed, "target_hp": target.current_hp})
    return [event]


def _resolve_improvise(actor: Combatant, action: dict, combatants: dict[str, Combatant]) -> list[dict]:
    """创意动作（v0）：DM 给 DC（默认 12），行动者掷敏捷检定；引擎只判成败，效果交 DM 叙述。"""
    dc = int(action.get("dc", 12))
    ability = Ability(action.get("ability", Ability.DEXTERITY))
    bonus = ability_check_bonus(actor, ability)

    if actor.is_player_controlled:
        resume_value = interrupt(build_interrupt_request(
            kind=InterruptType.ABILITY_CHECK,
            actor=actor,
            prompt=f"创意动作「{action.get('description', '')}」：掷 {ability.value}检定 d20 + {bonus}，对抗 DC {dc}",
            required_dice="d20",
            bonus=bonus,
        ))
        d20 = validate_d20(resume_value)
    else:
        d20 = _dice.d20()

    success = check_success(d20, bonus, dc)
    return [{
        "event": "improvise", "actor": actor.id, "description": action.get("description", ""),
        "d20": d20, "dc": dc, "success": success,
    }]


def _resolve_move(actor: Combatant, action: dict) -> list[dict]:
    """移动：改变所在区域（本版区域粒度，不算格子）。"""
    old_zone = actor.current_zone
    actor.current_zone = action.get("target_zone", old_zone)
    return [{
        "event": "move", "actor": actor.id,
        "from": old_zone, "to": actor.current_zone,
    }]


# ---------------------------------------------------------------------------
# 7. narrate（DM）
# ---------------------------------------------------------------------------
def narrate(state: CombatState) -> dict:
    """把本回合事件讲成故事。

    v0 用确定性模板生成叙述并通过 custom 流推给前端（复用现有 graph.invoke 的
    custom 事件通道）；日后可替换为 LLM（astream_agent_collect）。不改任何数值。
    """
    events = state.get("turn_events", []) or []
    combatants = state["combatants"]
    sentences = [_narrate_event(e, combatants) for e in events]
    narration = " ".join(s for s in sentences if s)

    writer = None
    try:
        writer = get_stream_writer()
    except Exception:  # 非图执行上下文（如单测直接调用）时无 writer
        writer = None
    if writer and narration:
        writer({"node": "narrate", "status": "start"})
        writer({"node": "narrate", "status": "streaming", "chunk": narration})
        writer({"node": "narrate", "status": "end"})

    log = _append_log(state, [{"event": "narration", "text": narration, "round": state.get("current_round")}])
    return {"combat_log": log}


def _name_of(combatants: dict[str, Combatant], cid: str | None) -> str:
    """取参战者名字，找不到时退化为 id 或「某人」。"""
    c = combatants.get(cid or "")
    return c.name if c else (cid or "某人")


def _narrate_event(e: dict, combatants: dict[str, Combatant]) -> str:
    """把一条结构化事件渲染成一句中文叙述。"""
    name = lambda cid: _name_of(combatants, cid)  # noqa: E731
    event_type = e.get("event")
    if event_type == "attack":
        if not e.get("hit"):
            return f"{name(e.get('actor'))}的{e.get('attack_name')}落空了。"
        crit = "重击！" if e.get("crit") else ""
        down = "，将其击倒！" if e.get("target_alive") is False else "。"
        return f"{crit}{name(e.get('actor'))}的{e.get('attack_name')}命中{name(e.get('target'))}，造成{e.get('damage', 0)}点伤害{down}"
    if event_type == "skill":
        if "heal" in e:
            return f"{name(e.get('actor'))}施展技能，恢复了{e.get('heal')}点生命。"
        return f"{name(e.get('actor'))}施展了一项技能。"
    if event_type == "item":
        if "heal" in e:
            return f"{name(e.get('actor'))}使用道具，为{name(e.get('target'))}恢复{e.get('heal')}点生命。"
        return f"{name(e.get('actor'))}使用了一件道具。"
    if event_type == "improvise":
        return f"{name(e.get('actor'))}尝试{e.get('description') or '一个临场动作'}，{'成功' if e.get('success') else '失败'}了。"
    if event_type == "move":
        return f"{name(e.get('actor'))}移动到了{e.get('to')}。"
    if event_type == "damage_over_time":
        return f"{name(e.get('actor'))}受到持续伤害{e.get('damage')}点。"
    if event_type == "skip":
        reason_text = {"surprised": "被突袭", "stunned": "眩晕"}.get(e.get("reason"), e.get("reason"))
        return f"{name(e.get('actor'))}因{reason_text}无法行动。"
    if event_type == "pass":
        return f"{name(e.get('actor'))}选择按兵不动。"
    return ""


# ---------------------------------------------------------------------------
# 8. check_end（引擎节点）+ 路由
# ---------------------------------------------------------------------------
def check_end(state: CombatState) -> dict:
    """判胜负，改写 `outcome`（条件由 `route_after_check` 只读路由）。"""
    combatants = state["combatants"]
    enemy_alive = any(c.is_alive for c in combatants.values() if c.faction == Faction.ENEMY)
    player_alive = any(c.is_alive for c in combatants.values() if c.faction == Faction.PLAYER)

    if not enemy_alive:
        outcome = CombatOutcome.PLAYERS_WIN
    elif not player_alive:
        outcome = CombatOutcome.PLAYERS_LOSE
    else:
        outcome = CombatOutcome.ONGOING

    logger.info("[check_end] 战斗结果=%s", outcome.value)
    return {"outcome": outcome}


def route_after_check(state: CombatState) -> str:
    """条件边路由：进行中→继续下一位；否则→结算。"""
    return "continue" if state["outcome"] == CombatOutcome.ONGOING else "end"


# ---------------------------------------------------------------------------
# 9. settle（引擎）
# ---------------------------------------------------------------------------
def settle(state: CombatState) -> dict:
    """结算并回到剧情：置结束阶段，发战利品，导出可写回世界库的数据。"""
    scene = state.get("scene_context", {}) or {}
    combatants = state["combatants"]

    writeback = {
        cid: {
            "current_hp": c.current_hp,
            "life_state": str(c.life_state.value),
            "conditions": [s.to_dict() for s in c.conditions],
            "inventory": [i.to_dict() for i in getattr(c, "inventory", [])],
        }
        for cid, c in combatants.items()
    }
    loot = scene.get("loot_table", []) if state["outcome"] == CombatOutcome.PLAYERS_WIN else []

    events = [{
        "event": "settle", "outcome": str(state["outcome"].value),
        "loot": loot,
    }]
    logger.info("[settle] 战斗结束 | 结果=%s", state["outcome"].value)
    return {
        "phase": CombatPhase.ENDED,
        "combat_log": _append_log(state, events),
        "scene_context": {**scene, "writeback": writeback, "granted_loot": loot},
    }
