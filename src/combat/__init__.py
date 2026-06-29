"""战斗引擎包。

把 docs/战斗 的设计落成可中断、可持久化的 LangGraph 子图：
- `dice` 掷骰、`rules` 规则结算（纯函数）；
- `nodes` 八个图节点、`interrupts` 玩家中断协议；
- `graph.build_combat_graph` 装配子图；
- `engine.CombatEngine` 对外门面（按房间驱动整场战斗）。
"""

from src.combat.dice import Dice, RollResult, parse_dice
from src.combat.engine import CombatEngine, room_thread_id
from src.combat.graph import build_combat_graph
from src.combat.rules import (
    AttackResult,
    ability_check_bonus,
    check_success,
    in_reach,
    resolve_attack,
    saving_throw_bonus,
)

__all__ = [
    "Dice",
    "RollResult",
    "parse_dice",
    "resolve_attack",
    "AttackResult",
    "check_success",
    "saving_throw_bonus",
    "ability_check_bonus",
    "in_reach",
    "build_combat_graph",
    "CombatEngine",
    "room_thread_id",
]
