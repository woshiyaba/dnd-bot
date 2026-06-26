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

from src.model.attack import Attack
from src.model.combatant import Character, Combatant, Monster, NPC, PlayerCharacter
from src.model.effects import Condition, InventoryItem, LearnedSkill
from src.model.enums import (
    Ability,
    ActionType,
    CombatOutcome,
    CombatPhase,
    ConditionType,
    DamageType,
    Faction,
    InterruptType,
    LifeState,
    Range,
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
_COMBAT_SERDE_WHITELIST = (
    Combatant, Monster, Character, PlayerCharacter, NPC,
    Attack, Condition, LearnedSkill, InventoryItem,
    Ability, ActionType, CombatOutcome, CombatPhase, ConditionType,
    DamageType, Faction, InterruptType, LifeState, Range,
)


def build_serde():
    """构造允许我方战斗模型反序列化的 JSON+ 序列化器。

    会话主图（src/session）把战斗子图当子图嵌入时，自身的 checkpointer 也需要
    用同一份白名单（DMState 里同样持久化这些战斗模型对象），故公开本函数复用。
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=list(_COMBAT_SERDE_WHITELIST))


# 向后兼容别名（旧调用方可能用了带下划线的私有名）
_build_serde = build_serde


def build_combat_graph(checkpointer: Any | None = None, *, embeddable: bool = False):
    """构建并编译战斗子图。

    参数:
        checkpointer: 缺省用自带 MemorySaver；要持久化整场战斗（多人、重启不丢档）时，
            传入 SqliteSaver / 自建 MySQL saver。
        embeddable: True 时**不挂任何 checkpointer**，编译成「可嵌入子图」——
            供会话主图（src/session）用包装节点 ``subgraph.invoke()`` 调用，由主图
            统一持有 checkpointer（中断会冒泡到主图，详见 docs/DM/01）。此模式下
            忽略 ``checkpointer`` 参数。
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

    # check_end 节点改写「outcome」，route_after_check 只读路由
    g.add_conditional_edges("check_end", route_after_check, {
        "continue": "next_turn",
        "end": "settle",
    })
    g.add_edge("settle", END)

    if embeddable:
        # 可嵌入子图：不挂 checkpointer，交由会话主图统一持有（中断冒泡到主图）
        return g.compile()

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver(serde=build_serde())

    return g.compile(checkpointer=checkpointer)
