"""战斗子图的节点实现。

每个节点读写 docs/战斗/01 定义的 `CombatState`，对照 docs/战斗/02 的流程：

    enter_combat → judge_surprise → narrate_opening → roll_initiative → next_turn
    → declare_action → resolve_action → narrate → check_end ─┐
              ▲────────────────(outcome==进行中)─────────────┘
              └──(否则)──► settle → END

设计原则：**规则归引擎，叙述归 DM，骰子归玩家**。
- 引擎节点 = 纯 Python 确定性结算；
- 玩家骰子 = `interrupt()` 收集（仅 `is_player_controlled` 的参战者）；
- 怪物/环境骰子 = 引擎用可复现随机源自动掷；
- DM 决策/叙述必须由真实 LLM 完成，不允许确定性模拟 DM 回答。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from src.character.features import (
    action_budget_for,
    extra_attack_budget_for,
    general_action_budget_for,
)
from src.character.progression import grant_experience
from src.combat.dice import current_engine_dice, reset_engine_dice
from src.combat.interrupts import (
    build_action_options,
    build_combat_view,
    build_interrupt_request,
    extract_damage,
    extract_roll_source,
    validate_d20,
)
from src.combat.rules import (
    ability_check_bonus,
    check_success,
    in_reach,
    resolve_attack,
    saving_throw_bonus,
)
from src.model.combat_state import CombatState, load_combatants
from src.model.combatant import Character, Combatant
from src.model.effects import Condition
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

# 可复现随机源：怪物/环境骰子（与 DM 骰子工具）共用 dice.py 的引擎骰子单例，
# 可在 enter_combat 用场景里的「random_seed」重置（见 reset_engine_dice）。


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


def _turn_action_budget(actor: Combatant) -> int:
    """按通用额外攻击和战士常驻动作如潮计算本回合行动次数。"""
    if not isinstance(actor, Character):
        return 1
    return action_budget_for(
        actor.class_id or "",
        actor.level,
        actor.features,
    )


def _clear_concentration_effects(
    combatants: dict[str, Combatant], skill_id: str, source_actor_id: str
) -> None:
    """移除某个专注技能产生的可追踪状态，并还原其数值修正。"""
    for combatant in combatants.values():
        removed = [
            condition
            for condition in combatant.conditions
            if condition.source_skill_id == skill_id
            and condition.source_actor_id == source_actor_id
        ]
        for condition in removed:
            if condition.stat == "ac":
                combatant.ac -= condition.amount
            if condition.stat == "attack_bonus":
                for attack in combatant.attacks:
                    attack.attack_bonus -= condition.amount
        combatant.conditions = [
            condition
            for condition in combatant.conditions
            if not (
                condition.source_skill_id == skill_id
                and condition.source_actor_id == source_actor_id
            )
        ]


def _check_concentration(
    state: CombatState,
    target: Combatant,
    damage: int,
    combatants: dict[str, Combatant],
) -> dict | None:
    """受伤后由引擎执行专注体质豁免，失败时清理来源效果。"""
    skill_id = target.concentration_skill_id
    if not skill_id or damage <= 0:
        return None
    if not target.is_alive:
        _clear_concentration_effects(combatants, skill_id, target.id)
        target.concentration_skill_id = None
        return {
            "event": "concentration_save",
            "actor": target.id,
            "skill_id": skill_id,
            "success": False,
            "reason": "down",
        }
    dc = max(10, damage // 2)
    bonus = saving_throw_bonus(target, Ability.CONSTITUTION)
    d20, source = _roll_d20_for(
        target,
        kind=InterruptType.SAVING_THROW,
        prompt=f"{target.name} 维持「{skill_id}」专注：体质豁免 d20 + {bonus}，DC {dc}",
        bonus=bonus,
        state=state,
    )
    success = check_success(d20, bonus, dc)
    if not success:
        _clear_concentration_effects(combatants, skill_id, target.id)
        target.concentration_skill_id = None
    return {
        "event": "concentration_save",
        "actor": target.id,
        "skill_id": skill_id,
        "d20": d20,
        "bonus": bonus,
        "dc": dc,
        "source": source,
        "success": success,
    }


# ---------------------------------------------------------------------------
# 1. enter_combat（引擎）
# ---------------------------------------------------------------------------
def enter_combat(state: CombatState) -> dict:
    """初始化战斗：加载参战者、摆好区域、清空工作区。"""
    scene = state.get("scene_context", {}) or {}
    random_seed = scene.get("random_seed")
    if random_seed is not None:
        reset_engine_dice(int(random_seed))

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
        "pending_action_plan": None,
        "actions_remaining": 0,
        "extra_attacks_remaining": 0,
        "attack_action_started": False,
        "action_feedback": None,
        "used_rule_actions": list(scene.get("used_session_rule_actions", []) or []),
        "committed_action_plans": [],
        "turn_events": [],
        "combat_log": list(state.get("combat_log", [])),
    }


# ---------------------------------------------------------------------------
# 2. judge_surprise（DM）
# ---------------------------------------------------------------------------
async def judge_surprise(state: CombatState) -> dict:
    """判定突袭。

    交给真实 DM LLM 做「潜行 vs 被动察觉」对抗判定；失败直接抛错。
    """
    scene = state.get("scene_context", {}) or {}
    combatants = state["combatants"]

    from src.combat import dm_bridge

    surprised = await dm_bridge.judge_surprise_llm(combatants, scene)

    for cid in surprised:
        combatants[cid].is_surprised = True

    events = [{"event": "surprise_check", "surprised": surprised}]
    logger.info("[judge_surprise] 被突袭=%s", surprised)
    return {
        "combatants": combatants,
        "phase": CombatPhase.SURPRISE,
        "combat_log": _append_log(state, events),
    }


async def narrate_opening(state: CombatState) -> dict:
    """由真实 DM 叙述已提交战斗的开场，不提前产生任何规则结果。"""
    from src.combat import dm_bridge

    narration = await dm_bridge.narrate_combat_opening_llm(
        state["combatants"],
        state.get("scene_context", {}) or {},
    )
    if not narration:
        raise RuntimeError("[combat.dm] 战斗开场叙述为空")
    return {
        "combat_log": _append_log(
            state,
            [{"event": "combat_opening", "text": narration, "round": 0}],
        )
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
    roll_sources: dict[str, str] = {}

    for c in combatants.values():
        if c.is_player_controlled:
            prompt = f"轮到 {c.name}，掷先攻：d20 + {c.effective_initiative_bonus}"
            resume_value = interrupt(
                build_interrupt_request(
                    kind=InterruptType.ROLL_INITIATIVE,
                    actor=c,
                    prompt=prompt,
                    required_dice="d20",
                    bonus=c.effective_initiative_bonus,
                    extra={"combat": build_combat_view(state, actor_id=c.id)},
                )
            )
            d20 = validate_d20(resume_value)
            roll_sources[c.id] = extract_roll_source(resume_value)
        else:
            d20 = current_engine_dice().d20()
            roll_sources[c.id] = "engine"
        c.initiative = d20 + c.effective_initiative_bonus

    # 降序排序；平手用敏捷调整值，再用引擎随机数打破
    order = sorted(
        combatants.values(),
        key=lambda c: (
            c.initiative,
            c.modifier(Ability.DEXTERITY),
            current_engine_dice().d20(),
        ),
        reverse=True,
    )
    initiative_order = [c.id for c in order]

    events = [
        {
            "event": "roll_initiative",
            "initiative_order": [
                {
                    "id": c.id,
                    "name": c.name,
                    "initiative": c.initiative,
                    "source": roll_sources[c.id],
                }
                for c in order
            ],
        }
    ]
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

        if isinstance(actor, Character):
            for skill in actor.skills:
                skill.tick_cooldown()

        # —— 回合开始结算：持续伤害 ——
        for s in list(actor.conditions):
            if s.kind == ConditionType.DAMAGE_OVER_TIME and s.amount > 0:
                dealt = actor.take_damage(s.amount)
                events.append(
                    _with_round(
                        state | {"current_round": rnd},
                        {
                            "event": "damage_over_time",
                            "actor": actor.id,
                            "damage": dealt,
                            "current_hp": actor.current_hp,
                        },
                    )
                )
                concentration = _check_concentration(
                    state | {"current_round": rnd}, actor, dealt, combatants
                )
                if concentration:
                    events.append(
                        _with_round(state | {"current_round": rnd}, concentration)
                    )

        was_stunned = actor.has_condition(ConditionType.STUNNED)
        actor.tick_conditions()

        if not actor.is_alive:
            events.append(
                {
                    "event": "down",
                    "actor": actor.id,
                    "reason": "damage_over_time",
                    "round": rnd,
                }
            )
            continue
        if actor.is_surprised and rnd == 1:
            events.append(
                {
                    "event": "skip",
                    "actor": actor.id,
                    "reason": "surprised",
                    "round": rnd,
                }
            )
            continue
        if was_stunned:
            events.append(
                {"event": "skip", "actor": actor.id, "reason": "stunned", "round": rnd}
            )
            continue
        break

    logger.info("[next_turn] 轮次=%d 指针=%d 行动者=%s", rnd, index, order[index])
    return {
        "combatants": combatants,
        "current_index": index,
        "current_round": rnd,
        "phase": CombatPhase.IN_TURN,
        "current_action": None,
        "pending_action_plan": None,
        "actions_remaining": (
            general_action_budget_for(actor.class_id or "", actor.level, actor.features)
            if isinstance(actor, Character)
            else 1
        ),
        "extra_attacks_remaining": (
            extra_attack_budget_for(actor.class_id or "", actor.level, actor.features)
            if isinstance(actor, Character)
            else 0
        ),
        "attack_action_started": False,
        "action_feedback": None,
        "turn_events": [],
        "combat_log": _append_log(state, events),
    }


# ---------------------------------------------------------------------------
# 5. declare_action（玩家中断 / DM）
# ---------------------------------------------------------------------------
async def declare_action(state: CombatState) -> dict:
    """声明行动与目标：玩家中断选择；怪物/NPC 由 DM 决策。

    怪物/NPC 决策必须交给真实 DM LLM（可读怪物卡、查规则）；失败直接抛错。
    """
    scene = state.get("scene_context", {}) or {}
    combatants = state["combatants"]
    actor = _current_actor(state)

    if actor.is_player_controlled:
        story_flags = scene.get("story_flags", {}) or {}
        options = build_action_options(
            actor,
            combatants,
            action_definitions=list(scene.get("action_definitions", []) or []),
            encounter_id=scene.get("encounter_id"),
            story_flags=[
                flag for flag, enabled in story_flags.items() if bool(enabled)
            ],
            used_rule_actions=list(state.get("used_rule_actions", []) or []),
            actions_remaining=int(state.get("actions_remaining", 1)),
            extra_attacks_remaining=int(state.get("extra_attacks_remaining", 0)),
            attack_action_started=bool(state.get("attack_action_started")),
        )
        feedback = state.get("action_feedback")
        prompt = f"轮到 {actor.name}，从当前选项中选择或描述本回合行动"
        if feedback:
            prompt = f"{feedback}\n{prompt}"
        resume_value = interrupt(
            build_interrupt_request(
                kind=InterruptType.DECLARE_ACTION,
                actor=actor,
                prompt=prompt,
                options=options,
                extra={"combat": build_combat_view(state, actor_id=actor.id)},
            )
        )
        current_action, feedback, declaration = await _normalize_player_action(
            resume_value,
            actor,
            combatants,
            options,
            scene,
        )
        update = {
            "current_action": current_action,
            "action_feedback": feedback,
        }
        if declaration:
            update["combat_log"] = _append_log(state, [declaration])
    else:
        from src.combat import dm_bridge

        current_action = await dm_bridge.decide_action_llm(
            actor,
            combatants,
            attack_only=int(state.get("actions_remaining", 0)) <= 0,
        )
        update = {"current_action": current_action, "action_feedback": None}

    logger.info("[declare_action] %s -> %s", actor.id, current_action)
    return update


async def _normalize_player_action(
    resume_value: Any,
    actor: Combatant,
    combatants: dict[str, Combatant],
    options: dict,
    scene: dict,
) -> tuple[dict, str | None, dict | None]:
    """校验玩家回报；自然语言只由真实 DM 映射到当前封闭选项。"""
    if not isinstance(resume_value, dict):
        return (
            {"action_type": ActionType.REJECTED.value},
            "没有收到可识别的行动，请重新选择或描述。",
            None,
        )

    from src.combat import dm_bridge

    action_type = resume_value.get("action_type")
    if action_type == ActionType.NATURAL_LANGUAGE.value:
        text = str(
            resume_value.get("description") or resume_value.get("text") or ""
        ).strip()
        if not text:
            return (
                {"action_type": ActionType.REJECTED.value},
                "行动描述不能为空，请说清楚这回合想做什么。",
                None,
            )
        declaration = {
            "event": "declaration",
            "actor_id": actor.id,
            "text": text,
        }
        decision = await dm_bridge.adjudicate_player_action_llm(
            actor,
            text,
            options,
            combatants,
            scene,
        )
        if not decision["accepted"]:
            return (
                {"action_type": ActionType.REJECTED.value},
                str(decision["reason"]),
                declaration,
            )
        action = dict(decision["action"])
        action["declared_text"] = text
        return action, None, declaration

    normalized = dm_bridge.validate_player_action(
        resume_value,
        options,
        combatants,
    )
    if normalized is None:
        return (
            {"action_type": ActionType.REJECTED.value},
            "该行动不在当前合法选项中，请重新选择。",
            None,
        )
    return normalized, None, None


def route_after_declare(state: CombatState) -> str:
    """非法自然语言行动不消耗回合，留在当前行动者重新声明。"""
    action = state.get("current_action") or {}
    if action.get("action_type") == ActionType.REJECTED.value:
        return "retry"
    if action.get("action_type") == ActionType.RULE_ACTION.value:
        return "rule_action"
    return "resolve"


async def prepare_rule_action(state: CombatState) -> dict:
    """调用真实 LLM 生成统一行动计划；本节点前不提交任何成本。"""
    actor = _current_actor(state)
    action = state.get("current_action") or {}
    action_id = str(action.get("action_id") or "")
    from src.combat.action_compiler import prepare_action_plan
    from src.combat.action_registry import combat_action_entries

    scene = state.get("scene_context", {}) or {}
    _, definitions = combat_action_entries(
        actor,
        state["combatants"],
        canon_definitions=list(scene.get("action_definitions", []) or []),
        story_flags=dict(scene.get("story_flags", {}) or {}),
        encounter_id=scene.get("encounter_id"),
        used_action_ids=list(state.get("used_rule_actions", []) or []),
    )
    definition = definitions.get(action_id)
    if definition is None:
        raise ValueError(f"规则行动 «{action_id}» 不属于当前合法定义")

    target_ids = [str(value) for value in action.get("target_ids", [])]
    if not target_ids and action.get("target_id"):
        target_ids = [str(action["target_id"])]
    plan = await prepare_action_plan(
        definition=definition,
        actor=actor,
        targets=state["combatants"],
        selected_target_ids=target_ids,
        scope="combat",
        context={
            "round": int(state.get("current_round", 1)),
            "encounter_id": scene.get("encounter_id"),
        },
    )
    return {"pending_action_plan": plan}


def commit_rule_action(state: CombatState) -> dict:
    """在独立无中断节点提交统一行动成本，保证恢复时只扣除一次。"""
    from src.combat.action_executor import commit_action_cost

    actor = _current_actor(state)
    plan = state.get("pending_action_plan")
    if not isinstance(plan, dict):
        raise ValueError("规则行动缺少已校验计划")
    used, committed, event = commit_action_cost(
        actor,
        plan,
        list(state.get("used_rule_actions", []) or []),
        list(state.get("committed_action_plans", []) or []),
    )
    event = _with_round(state, event)
    return {
        "combatants": state["combatants"],
        "used_rule_actions": used,
        "committed_action_plans": committed,
        "turn_events": [event],
        "combat_log": _append_log(state, [event]),
    }


def execute_rule_action(state: CombatState) -> dict:
    """执行已提交的统一计划并消费本回合通用动作。"""
    from src.combat.action_executor import execute_combat_plan

    actor = _current_actor(state)
    plan = state.get("pending_action_plan")
    if not isinstance(plan, dict):
        raise ValueError("规则行动缺少已校验计划")
    events = [
        _with_round(state, event) for event in execute_combat_plan(state, actor, plan)
    ]
    declared_text = (state.get("current_action") or {}).get("declared_text")
    if declared_text:
        for event in events:
            event["declared_text"] = declared_text
    remaining = max(0, int(state.get("actions_remaining", 1)) - 1)
    logger.info(
        "[execute_rule_action] %s action=%s 事件=%s",
        actor.id,
        plan.get("definition_id"),
        [event.get("event") for event in events],
    )
    return {
        "combatants": state["combatants"],
        "actions_remaining": remaining,
        "pending_action_plan": None,
        "action_feedback": None,
        "turn_events": events,
        "combat_log": _append_log(state, events),
    }


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
        events = _resolve_attack(state, actor, action, combatants)
    elif action_type == ActionType.MOVE.value:
        events = _resolve_move(actor, action)
    else:
        events = [{"event": "pass", "actor": actor.id}]

    declared_text = action.get("declared_text")
    events = [_with_round(state, e) for e in events]
    if declared_text:
        for event in events:
            event["declared_text"] = declared_text
    logger.info(
        "[resolve_action] %s 事件=%s", actor.id, [e.get("event") for e in events]
    )
    remaining = max(0, int(state.get("actions_remaining", 1)))
    extra_attacks = max(0, int(state.get("extra_attacks_remaining", 0)))
    attack_started = bool(state.get("attack_action_started"))
    if remaining > 0:
        remaining -= 1
        if action_type == ActionType.ATTACK.value:
            attack_started = True
    elif (
        action_type == ActionType.ATTACK.value and attack_started and extra_attacks > 0
    ):
        extra_attacks -= 1
    if action_type == ActionType.PASS.value:
        remaining = 0
        extra_attacks = 0
    return {
        "combatants": combatants,
        "actions_remaining": remaining,
        "extra_attacks_remaining": extra_attacks,
        "attack_action_started": attack_started,
        "pending_action_plan": None,
        "action_feedback": None,
        "turn_events": events,
        "combat_log": _append_log(state, events),
    }


def _resolve_attack(
    state: CombatState,
    actor: Combatant,
    action: dict,
    combatants: dict[str, Combatant],
) -> list[dict]:
    """攻击结算：掷命中 → 判定 → 掷伤害 → 扣 HP，必要时置倒下。"""
    weapon = next(
        (a for a in actor.attacks if a.name == action.get("attack_name")), None
    )
    if weapon is None and actor.attacks:
        weapon = actor.attacks[0]
    target = combatants.get(action.get("target_id", ""))

    if weapon is None or target is None or not target.is_alive:
        return [
            {
                "event": "invalid_attack",
                "actor": actor.id,
                "target": action.get("target_id"),
            }
        ]
    if not in_reach(actor, target, weapon.is_ranged):
        return [
            {
                "event": "out_of_reach",
                "actor": actor.id,
                "target": target.id,
                "attack_name": weapon.name,
            }
        ]

    # —— 命中骰：玩家中断（可一并报伤害）；怪物引擎掷 ——
    player_damage: int | None = None
    attack_source = "engine"
    damage_source = "engine"
    if actor.is_player_controlled:
        resume_value = interrupt(
            build_interrupt_request(
                kind=InterruptType.ATTACK_ROLL,
                actor=actor,
                prompt=f"{actor.name} 用「{weapon.name}」攻击 {target.name}：掷 d20 + {weapon.attack_bonus}",
                required_dice="d20",
                bonus=weapon.attack_bonus,
                extra={
                    "damage_dice": weapon.damage_dice,
                    "combat": build_combat_view(state, actor_id=actor.id),
                },
            )
        )
        d20 = validate_d20(resume_value)
        attack_source = extract_roll_source(resume_value)
        player_damage = extract_damage(resume_value)
        if player_damage is not None:
            damage_source = attack_source
    else:
        d20 = current_engine_dice().d20()

    result = resolve_attack(d20, weapon.attack_bonus, target.ac)
    event: dict = {
        "event": "attack",
        "actor": actor.id,
        "target": target.id,
        "attack_name": weapon.name,
        "d20": d20,
        "source": attack_source,
        "hit": result.hit,
        "crit": result.crit,
    }

    if not result.hit:
        return [event]

    # —— 伤害骰 ——
    if result.crit:
        # 重击需翻倍骰数：玩家补一次伤害掷骰中断；怪物引擎翻倍掷
        if actor.is_player_controlled:
            resume_value = interrupt(
                build_interrupt_request(
                    kind=InterruptType.DAMAGE_ROLL,
                    actor=actor,
                    prompt=f"重击！把 {weapon.damage_dice} 的骰子数翻倍掷，报伤害总和",
                    required_dice=weapon.damage_dice,
                    extra={
                        "crit": True,
                        "combat": build_combat_view(state, actor_id=actor.id),
                    },
                )
            )
            damage = extract_damage(resume_value)
            damage_source = extract_roll_source(resume_value)
            if damage is None:
                damage = current_engine_dice().roll(weapon.damage_dice, crit=True).total
                damage_source = "engine"
        else:
            damage = current_engine_dice().roll(weapon.damage_dice, crit=True).total
    else:
        if actor.is_player_controlled:
            damage = (
                player_damage
                if player_damage is not None
                else current_engine_dice().roll(weapon.damage_dice).total
            )
            if player_damage is None:
                damage_source = "engine"
        else:
            damage = current_engine_dice().roll(weapon.damage_dice).total

    dealt = target.take_damage(damage)
    event.update(
        {
            "damage": dealt,
            "damage_source": damage_source,
            "damage_type": str(weapon.damage_type.value),
            "target_hp": target.current_hp,
            "target_alive": target.is_alive,
        }
    )
    events = [event]
    concentration = _check_concentration(state, target, dealt, combatants)
    if concentration:
        events.append(concentration)
    return events


def _roll_d20_for(
    roller: Combatant,
    *,
    kind: InterruptType,
    prompt: str,
    bonus: int,
    state: CombatState,
) -> tuple[int, str]:
    """按控制权取得一颗原始 d20；玩家中断、其他参战者引擎掷。"""
    if roller.is_player_controlled:
        resume_value = interrupt(
            build_interrupt_request(
                kind=kind,
                actor=roller,
                prompt=prompt,
                required_dice="d20",
                bonus=bonus,
                extra={"combat": build_combat_view(state, actor_id=roller.id)},
            )
        )
        return validate_d20(resume_value), extract_roll_source(resume_value)
    return current_engine_dice().d20(), "engine"


def _resolve_move(actor: Combatant, action: dict) -> list[dict]:
    """移动：改变所在区域（本版区域粒度，不算格子）。"""
    old_zone = actor.current_zone
    actor.current_zone = action.get("target_zone", old_zone)
    return [
        {
            "event": "move",
            "actor": actor.id,
            "from": old_zone,
            "to": actor.current_zone,
        }
    ]


# ---------------------------------------------------------------------------
# 7. narrate（DM）
# ---------------------------------------------------------------------------
async def narrate(state: CombatState) -> dict:
    """把本回合事件讲成故事。不改任何数值。

    交给真实 DM LLM 流式生成叙述（token 经 custom 通道实时推送）。失败直接抛错。
    """
    events = state.get("turn_events", []) or []
    combatants = state["combatants"]

    if events:
        from src.combat import dm_bridge

        narration = await dm_bridge.narrate_llm(
            events, combatants, state.get("current_round")
        )
        if narration:
            log = _append_log(
                state,
                [
                    {
                        "event": "narration",
                        "text": narration,
                        "round": state.get("current_round"),
                    }
                ],
            )
            return {"combat_log": log}

    return {"combat_log": state.get("combat_log", [])}


# ---------------------------------------------------------------------------
# 8. check_end（引擎节点）+ 路由
# ---------------------------------------------------------------------------
def check_end(state: CombatState) -> dict:
    """判胜负，改写 `outcome`（条件由 `route_after_check` 只读路由）。"""
    combatants = state["combatants"]
    enemy_alive = any(
        c.is_alive for c in combatants.values() if c.faction == Faction.ENEMY
    )
    player_alive = any(
        c.is_alive for c in combatants.values() if c.faction == Faction.PLAYER
    )

    if not enemy_alive:
        outcome = CombatOutcome.PLAYERS_WIN
    elif not player_alive:
        outcome = CombatOutcome.PLAYERS_LOSE
    else:
        outcome = CombatOutcome.ONGOING

    logger.info("[check_end] 战斗结果=%s", outcome.value)
    return {"outcome": outcome}


def route_after_check(state: CombatState) -> str:
    """条件边路由：胜负结算、同角色剩余行动或推进下一位。"""
    if state["outcome"] != CombatOutcome.ONGOING:
        return "end"
    has_general_action = int(state.get("actions_remaining", 0)) > 0
    has_extra_attack = (
        bool(state.get("attack_action_started"))
        and int(state.get("extra_attacks_remaining", 0)) > 0
    )
    if (has_general_action or has_extra_attack) and _current_actor(state).is_alive:
        return "same_turn"
    return "next_turn"


# ---------------------------------------------------------------------------
# 9. settle（引擎）
# ---------------------------------------------------------------------------
def settle(state: CombatState) -> dict:
    """结算并回到剧情：置结束阶段，发战利品，导出可写回世界库的数据。"""
    scene = state.get("scene_context", {}) or {}
    combatants = state["combatants"]
    xp_reward = max(0, int(scene.get("xp_reward", 0)))
    growth: dict[str, dict[str, Any]] = {}
    if state["outcome"] == CombatOutcome.PLAYERS_WIN and xp_reward > 0:
        for combatant_id, combatant in combatants.items():
            if isinstance(combatant, Character):
                growth[combatant_id] = grant_experience(combatant, xp_reward)

    writeback = {
        cid: {
            "current_hp": c.current_hp,
            "max_hp": c.max_hp,
            "temporary_hp": c.temporary_hp,
            "ac": c.ac,
            "life_state": str(c.life_state.value),
            "conditions": [s.to_dict() for s in c.conditions],
            "inventory": [i.to_dict() for i in getattr(c, "inventory", [])],
            **(
                {
                    "level": c.level,
                    "experience": c.experience,
                    "pending_ability_points": c.pending_ability_points,
                    "strength": c.strength,
                    "dexterity": c.dexterity,
                    "constitution": c.constitution,
                    "intelligence": c.intelligence,
                    "wisdom": c.wisdom,
                    "charisma": c.charisma,
                    "skills": [skill.to_dict() for skill in c.skills],
                    "features": list(c.features),
                }
                if isinstance(c, Character)
                else {}
            ),
        }
        for cid, c in combatants.items()
    }
    loot = (
        scene.get("loot_table", [])
        if state["outcome"] == CombatOutcome.PLAYERS_WIN
        else []
    )

    events = [
        {
            "event": "settle",
            "outcome": str(state["outcome"].value),
            "loot": loot,
            "xp_reward": xp_reward,
            "growth": growth,
        }
    ]
    logger.info("[settle] 战斗结束 | 结果=%s", state["outcome"].value)
    return {
        "phase": CombatPhase.ENDED,
        "combat_log": _append_log(state, events),
        "scene_context": {
            **scene,
            "writeback": writeback,
            "granted_loot": loot,
            "growth": growth,
        },
    }
