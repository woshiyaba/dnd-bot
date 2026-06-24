"""战斗引擎包。

把 docs/战斗 的设计落成可中断、可持久化的 LangGraph 子图：
- `dice` 掷骰、`rules` 规则结算（纯函数）；
- `nodes` 八个图节点、`interrupts` 玩家中断协议；
- `graph.build_combat_graph` 装配子图；
- `engine.CombatEngine` 对外门面（按房间驱动整场战斗）。
"""

from src.combat.dice import 骰子, 掷骰结果, 解析骰子
from src.combat.engine import CombatEngine, 房间线程id
from src.combat.graph import build_combat_graph
from src.combat.rules import (
    判定攻击,
    判定检定,
    攻击判定,
    技能加值,
    豁免加值,
    够得着,
)

__all__ = [
    "骰子", "掷骰结果", "解析骰子",
    "判定攻击", "攻击判定", "判定检定", "豁免加值", "技能加值", "够得着",
    "build_combat_graph",
    "CombatEngine", "房间线程id",
]
