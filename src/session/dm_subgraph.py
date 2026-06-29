"""DM 子图：中央 DM 的一个「对话回合」。

落实 docs/DM/01-中央DM主图方案.md §3：

    START → perceive → dm_decide ─┬─(reply)──────────────────────────► END(next=wait)
                                  ├─(check)→ await_roll(中断)
                                  │           → resolve_check(引擎)
                                  │           → narrate_result(DM) ──► END(next=wait)
                                  └─(combat)─────────────────────────► END(next=combat)

设计要点（与三个验证探针一致，见方案 §二）：
- **dm_decide 是独立节点**（跑 LLM/启发式决策）；**await_roll 是独立的中断节点**。
  这样玩家报骰恢复时，langgraph 从中断节点续跑，dm_decide 不会重跑（不重复调用 LLM）。
- 玩家明检定的 d20 只能由玩家经 ``interrupt()`` 报，**加值与成败一律引擎算**（resolve_check），
  守住「规则归引擎」。DM 只产出检定规格（属性/DC），不裁定成败。

本模块属 ``session`` 层，可同时依赖 ``combat``（规则/中断）与 ``dm``（世界桥接）。
编译为**可嵌入子图**（不挂 checkpointer），由会话主图统一持有 checkpointer。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from src.combat.interrupts import build_interrupt_request, validate_d20
from src.combat.rules import ability_check_bonus, check_success, saving_throw_bonus
from src.dm import world_bridge
from src.model.combatant import Combatant
from src.model.dm_state import DMState
from src.model.enums import Ability, InterruptType
from src.session import story_nodes
from src.session.common import (
    llm_enabled,
    log_event,
)  # 共享工具（也供 graph.py 沿用导入）

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. perceive（引擎·轻量）：把玩家这步纳入对话，清空上回合工作区
# ---------------------------------------------------------------------------
def perceive(state: DMState) -> dict:
    """组装本回合上下文：把玩家输入并入对话历史，清空决策工作区。

    纯、廉价、确定性——本节点在任何中断**之前**完成并存档，恢复时不会重跑。
    """
    user_input = state.get("user_input", "") or ""
    messages = list(state.get("messages", []))
    if user_input:
        messages.append({"role": "user", "content": user_input})
    return {
        "messages": messages,
        # 清空上一回合的工作区，避免脏读
        "intent": "",
        "say": "",
        "pending_check": None,
        "last_check": None,
        "combat_request": None,
        "world_writes": None,
        "next": "wait",
    }


# ---------------------------------------------------------------------------
# 2. dm_decide（DM 智能体）：决定本回合意图
# ---------------------------------------------------------------------------
async def dm_decide(state: DMState) -> dict:
    """让 DM 读局面决定意图：reply / player_check / start_combat。

    昂贵且非确定（可能调 LLM）——必须独立成节点，以便恢复时不重跑（见模块文档）。
    """
    decision = await world_bridge.decide_turn(
        state.get("user_input", ""),
        state.get("scene", {}),
        state.get("party", {}),
        messages=state.get("messages", []),
        use_llm=llm_enabled(state),
        beat_brief=story_nodes.beat_brief_for(state),  # 当前拍骨架：让叙述长在骨架上
        stuck_hint=story_nodes.stuck_hint_for(state),  # 卡关兜底：空转太久时注入提示
    )
    intent = decision["intent"]
    writes = (
        decision.get("world_writes") or {}
    )  # DM 声明的世界写入，留给 evaluate_advancement 消费
    logger.info("[dm_decide] 意图=%s 世界写入=%s", intent, list(writes.keys()) or "无")

    if intent == "player_check":
        return {
            "intent": intent,
            "pending_check": decision["check"],
            "world_writes": writes,
            "next": "wait",
        }
    if intent == "start_combat":
        return {
            "intent": intent,
            "combat_request": decision["encounter"],
            "world_writes": writes,
            "next": "combat",
        }
    # reply
    return {
        "intent": intent,
        "say": decision.get("say", ""),
        "world_writes": writes,
        "next": "wait",
    }


def route_after_decide(state: DMState) -> str:
    """按 dm_decide 的意图分流。"""
    intent = state.get("intent")
    if intent == "player_check":
        return "check"
    if intent == "start_combat":
        return "combat"
    return "reply"


# ---------------------------------------------------------------------------
# 3a. reply 分支：直接把 DM 的话推给玩家
# ---------------------------------------------------------------------------
def narrate_reply(state: DMState) -> dict:
    """把 ``intent=reply`` 时 DM 已生成的话推给前端，并记一条对话。"""
    say = state.get("say", "") or ""
    world_bridge.narrate_reply(say)
    messages = list(state.get("messages", []))
    if say:
        messages.append({"role": "dm", "content": say})
    return {
        "messages": messages,
        "campaign_log": log_event(state, {"event": "narration", "text": say}),
    }


# ---------------------------------------------------------------------------
# 3b. check 分支：中断收玩家 d20 → 引擎判定 → 叙述
# ---------------------------------------------------------------------------
def _check_actor(state: DMState) -> Combatant | None:
    """取本次检定的玩家角色对象。"""
    check = state.get("pending_check") or {}
    return (state.get("party") or {}).get(check.get("actor_id"))


def _check_bonus(actor: Combatant, check: dict) -> int:
    """引擎算检定/豁免加值（不掷骰）。"""
    ability = Ability(check["ability"])
    if check.get("kind") == InterruptType.SAVING_THROW.value:
        return saving_throw_bonus(actor, ability)
    return ability_check_bonus(actor, ability, proficient=bool(check.get("proficient")))


def await_roll(state: DMState) -> dict:
    """中断点：向玩家要一个 d20 原始值（明骰）。

    只负责发中断、收原始骰；加值与成败留给 resolve_check（规则归引擎）。
    """
    check = state.get("pending_check") or {}
    actor = _check_actor(state)
    if actor is None:  # 兜底：无角色可掷 → 跳过检定
        return {"last_check": None}

    bonus = _check_bonus(actor, check)
    kind = InterruptType(check.get("kind", InterruptType.ABILITY_CHECK.value))
    resume_value = interrupt(
        build_interrupt_request(
            kind=kind,
            actor=actor,
            prompt=check.get("prompt")
            or f"请掷 d20（{check['ability']} 检定，DC {check['dc']}）",
            required_dice="d20",
            bonus=bonus,
        )
    )
    return {"last_check": {"_raw_roll": resume_value}}


def resolve_check(state: DMState) -> dict:
    """引擎结算：d20 + 加值 对比 DC，判成败。"""
    check = state.get("pending_check") or {}
    actor = _check_actor(state)
    raw = (state.get("last_check") or {}).get("_raw_roll")
    if actor is None or not check:
        return {"last_check": None}

    d20 = validate_d20(raw)
    bonus = _check_bonus(actor, check)
    total = d20 + bonus
    success = check_success(d20, bonus, check["dc"])
    result = {
        "actor_id": actor.id,
        "actor_name": actor.name,
        "ability": check["ability"],
        "kind": check.get("kind"),
        "dc": check["dc"],
        "d20": d20,
        "bonus": bonus,
        "total": total,
        "success": success,
    }
    logger.info(
        "[resolve_check] %s %s+%s=%s vs DC%s → %s",
        actor.name,
        d20,
        bonus,
        total,
        check["dc"],
        "成功" if success else "失败",
    )
    return {
        "last_check": result,
        "campaign_log": log_event(state, {"event": "ability_check", **result}),
    }


async def narrate_result(state: DMState) -> dict:
    """叙述检定结果（成功→是,然后…；失败→不,但是…）。"""
    result = state.get("last_check") or {}
    if not result or "success" not in result:
        return {}
    # 把「玩家尝试做的事 + 当前场景 + 对话上文」一并交给 DM，叙述才会紧扣动作、贴合场景并承接上文
    check = state.get("pending_check") or {}
    action = check.get("prompt") or check.get("reason")
    text = await world_bridge.narrate_result(
        result,
        use_llm=llm_enabled(state),
        action=action,
        scene=state.get("scene"),
        messages=state.get("messages"),
    )
    messages = list(state.get("messages", []))
    messages.append({"role": "dm", "content": text})
    return {
        "messages": messages,
        "campaign_log": log_event(state, {"event": "narration", "text": text}),
    }


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_dm_subgraph():
    """构建并编译 DM 子图（可嵌入：不挂 checkpointer，中断冒泡到会话主图）。"""
    g = StateGraph(DMState)
    g.add_node("perceive", perceive)
    g.add_node("dm_decide", dm_decide)
    g.add_node("narrate_reply", narrate_reply)
    g.add_node("await_roll", await_roll)
    g.add_node("resolve_check", resolve_check)
    g.add_node("narrate_result", narrate_result)

    g.add_edge(START, "perceive")
    g.add_edge("perceive", "dm_decide")
    g.add_conditional_edges(
        "dm_decide",
        route_after_decide,
        {
            "reply": "narrate_reply",
            "check": "await_roll",
            "combat": END,  # 交给会话主图路由进战斗子图（next=combat）
        },
    )
    g.add_edge("narrate_reply", END)
    g.add_edge("await_roll", "resolve_check")
    g.add_edge("resolve_check", "narrate_result")
    g.add_edge("narrate_result", END)

    return g.compile()
