"""LangGraph 图状态 `CombatState` 及参战者加载工厂。

对应 docs/战斗/01-战斗状态定义.md 第 2 节：整场战斗只有这一个状态对象，
被 checkpointer 按 thread_id 持久化。参战者以模型对象存放在 `combatants` 里
（唯一真相源），其余为回合调度、阶段、工作区与输出。
"""

from __future__ import annotations

from typing import Any, TypedDict

from src.model.combatant import Combatant, Monster, NPC, PlayerCharacter
from src.model.enums import CombatOutcome, CombatPhase, Faction


class CombatState(TypedDict, total=False):
    """整场战斗的图状态。"""

    # —— 参战者表（唯一真相源；HP/状态/存活全在这里）——
    combatants: dict[str, Combatant]   # id -> 参战者模型对象

    # —— 回合调度 ——
    initiative_order: list[str]        # 先攻顺序：排好序的 combatant id（高→低）
    current_index: int                 # 当前指针：指向先攻顺序里轮到谁；-1 表示尚未开始
    current_round: int                 # 当前轮次：一轮 = 所有人各行动一次

    # —— 阶段与结果 ——
    phase: CombatPhase                 # 阶段
    outcome: CombatOutcome             # 战斗结果

    # —— 本回合工作区（节点间传递，回合开始清空）——
    current_action: dict | None        # 声明行动节点产出：{action_type, target_id, ...}
    turn_events: list[dict]            # 本回合事件：结算节点产出的结构化事件，喂给 DM 叙述

    # —— 输出 ——
    combat_log: list[dict]             # 战斗日志：全场事件流（前端回放 + DM 上下文）
    scene_context: dict                # 场景上下文：触发战斗时带入（参战者、地点、战利品表…）


# ---------------------------------------------------------------------------
# 参战者加载工厂：把「场景上下文」里的卡面条目造成对应的模型子类
# ---------------------------------------------------------------------------
_TYPE_MAP = {
    "player": PlayerCharacter,
    "player_character": PlayerCharacter,
    "monster": Monster,
    "npc": NPC,
}


def load_combatant(entry: dict[str, Any]) -> Combatant:
    """从场景条目构造参战者模型。

    条目格式::

        {
          "type": "player" | "monster" | "npc",
          "controller": user_id | None,    # 玩家专用，对接 WebSocket 推送
          "faction": "player" | "enemy" | None,  # 可选，覆盖类型默认阵营
          "card": { ...英文键卡面... },
        }

    返回已带好运行时取向（阵营/是否玩家控制/操控者）的模型对象。
    """
    type_name = str(entry.get("type", "monster"))
    model_cls: type[Combatant] = _TYPE_MAP.get(type_name, Monster)
    card = entry.get("card", entry)

    instance = model_cls.from_card(card)

    # 运行时取向：显式条目优先，否则用模型子类默认值
    if entry.get("controller") is not None:
        instance.controller = entry["controller"]
    if entry.get("faction"):
        instance.faction = Faction(entry["faction"])
    return instance


def load_combatants(scene_context: dict) -> dict[str, Combatant]:
    """读取场景上下文里的「combatants」列表，构造 id -> 模型 的字典。"""
    table: dict[str, Combatant] = {}
    for entry in scene_context.get("combatants", []):
        instance = load_combatant(entry)
        table[instance.id] = instance
    return table
