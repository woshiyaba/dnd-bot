"""战斗子图的装配（StateGraph）。

把 docs/战斗/02 的流程落成 `StateGraph(CombatState)`：

    START → enter_combat → judge_surprise → roll_initiative → next_turn
    → declare_action → resolve_action → narrate → check_end
    check_end ─(进行中)→ next_turn
    check_end ─(否则)──→ settle → END

可中断、可持久化：玩家骰子靠 `interrupt()` 挂起，按 `thread_id`（建议 `combat:{房间id}`）
用 checkpointer 存档。默认 MemorySaver（单进程跑通用）；多人/重启场景换持久化版。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.model.attack import 攻击手段
from src.model.combatant import 参战者, 怪物, 玩家角色, 角色, 非玩家角色
from src.model.effects import 已学技能, 状态效果, 背包道具
from src.model.enums import (
    伤害类型,
    存活状态,
    战斗结果,
    战斗阶段,
    状态类型,
    射程,
    阵营,
)

from src.combat.nodes import (
    check_end,
    declare_action,
    enter_combat,
    judge_surprise,
    narrate,
    next_turn,
    resolve_action,
    roll_initiative,
    route_after_check,
    settle,
)
from src.model.combat_state import CombatState

# 这些自定义模型会被 checkpointer 以 msgpack 持久化进 CombatState；
# 显式登记为允许反序列化的类型，避免 LangGraph 未来版本拦截（并消除告警）。
_战斗序列化白名单 = (
    参战者, 怪物, 角色, 玩家角色, 非玩家角色,
    攻击手段, 状态效果, 已学技能, 背包道具,
    伤害类型, 存活状态, 状态类型, 射程, 阵营, 战斗阶段, 战斗结果,
)


def _构造serde():
    """构造允许我方战斗模型反序列化的 JSON+ 序列化器。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=list(_战斗序列化白名单))


def build_combat_graph(checkpointer: Any | None = None):
    """构建并编译战斗子图。

    checkpointer 缺省用 MemorySaver；要持久化整场战斗（多人、重启不丢档）时，
    传入 SqliteSaver / 自建 MySQL saver。
    """
    g = StateGraph(CombatState)

    g.add_node("enter_combat", enter_combat)
    g.add_node("judge_surprise", judge_surprise)
    g.add_node("roll_initiative", roll_initiative)
    g.add_node("next_turn", next_turn)
    g.add_node("declare_action", declare_action)
    g.add_node("resolve_action", resolve_action)
    g.add_node("narrate", narrate)
    g.add_node("check_end", check_end)
    g.add_node("settle", settle)

    g.add_edge(START, "enter_combat")
    g.add_edge("enter_combat", "judge_surprise")
    g.add_edge("judge_surprise", "roll_initiative")
    g.add_edge("roll_initiative", "next_turn")
    g.add_edge("next_turn", "declare_action")
    g.add_edge("declare_action", "resolve_action")
    g.add_edge("resolve_action", "narrate")
    g.add_edge("narrate", "check_end")

    # check_end 节点改写「战斗结果」，route_after_check 只读路由
    g.add_conditional_edges("check_end", route_after_check, {
        "continue": "next_turn",
        "end": "settle",
    })
    g.add_edge("settle", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver(serde=_构造serde())

    return g.compile(checkpointer=checkpointer)
