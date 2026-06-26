"""会话主图状态 `DMState` 及世界场景约定。

`DMState` 是比 `CombatState`（见 combat_state.py）更上层的状态：它表示「一整局冒险」
——玩家与 DM 的对话、当前世界场景、玩家角色册（HP/物品跨场景延续），以及触发战斗 /
等玩家检定时的工作区。被会话主图的 checkpointer 按 `thread_id`（建议 `session:{room}`）持久化。

设计原则（沿用 model 层约束）：
- model 层**不依赖** LangGraph / combat / dm；这里只放数据形状与纯函数。
- 战斗只是冒险的一部分：战斗触发时，本状态把「遭遇」交给战斗子图，战斗结束后把结果
  （HP/战利品/伤亡）折回这里，玩家继续只与 DM 对话。
"""

from __future__ import annotations

from typing import Any, TypedDict

from src.model.combatant import Combatant, PlayerCharacter
from src.model.combat_state import load_combatant


class DMState(TypedDict, total=False):
    """整局冒险的图状态（会话主图）。"""

    # —— 对话 ——
    messages: list[dict]          # 对话历史：[{"role": "user"|"dm", "content": str}]
    user_input: str               # 本回合玩家输入（perceive 读取，dm_decide 据此决策）
    user_id: str                  # 当前玩家 user_id（单人；多人二期）
    room_id: str                  # 房间/局 id，派生 thread_id

    # —— 世界 ——
    scene: dict                   # 当前世界场景（WorldScene，见模块文档）
    party: dict[str, Combatant]   # 玩家角色册：pc_id -> 角色对象（HP/物品跨场景延续，唯一真相源）

    # —— DM 决策工作区（每回合刷新）——
    intent: str                   # dm_decide 产出的意图：reply | player_check | start_combat
    say: str                      # reply 文本：DM 面向玩家要说的话
    pending_check: dict | None    # player_check 规格：{actor_id, ability, dc, kind, proficient, prompt, reason}
    last_check: dict | None       # 检定结算结果：{actor_id, ability, dc, d20, bonus, total, success}
    combat_request: dict | None   # start_combat 产出的遭遇：{monsters:[卡面条目], surprised, loot_table, random_seed}
    last_combat: dict | None      # 战斗结算回灌：{outcome, granted_loot, casualties, ...}

    # —— 路由信号 ——
    next: str                     # 会话主图条件路由：wait（等玩家下一条消息）| combat（进战斗子图）

    # —— 输出 ——
    campaign_log: list[dict]      # 全程世界事件流（前端回放 + DM 长期上下文）


# 一份「世界场景」WorldScene 约定（scene 字段的形状，供 DM 感知环境）::
#
#     {
#       "location": "废弃神殿·前厅",            # 地点名
#       "description": "霉味弥漫，断裂的石柱…",   # 环境描述（DM 维护）
#       "actors": [                            # 在场 NPC / 潜在敌人
#         {"actor_id": "goblin_1", "name": "哥布林", "disposition": "hostile",
#          "type": "monster", "card": { ...英文键卡面... }},
#       ],
#       "exits": ["东门", "地下室"],            # 可去的出口
#       "flags": {"火盆已点燃": true},          # 场景开关
#       "threat": "潜在伏击" | None,            # 当前威胁提示
#     }
#
# disposition 取值：hostile（敌意）| neutral（中立）| friendly（友好）。
# actors 里 disposition=hostile 的条目，触发战斗时可被 DM 选为敌方参战者。


def load_party(scene_context: dict) -> dict[str, Combatant]:
    """从初始场景上下文里的 `party` 列表构造玩家角色册（id -> 角色对象）。

    每个条目格式同 combat_state.load_combatant 的卡面条目（type/controller/card），
    一般 type 为 ``player``。返回的对象在战斗内外共用，HP/物品跨场景延续。
    """
    party: dict[str, Combatant] = {}
    for entry in scene_context.get("party", []):
        instance = load_combatant(entry)
        party[instance.id] = instance
    return party


def hostile_actors(scene: dict) -> list[dict]:
    """取场景里所有「敌意」在场者条目（用于触发战斗时组装敌方）。"""
    return [
        a for a in (scene or {}).get("actors", [])
        if a.get("disposition") == "hostile"
    ]


def player_characters(party: dict[str, Combatant]) -> list[PlayerCharacter]:
    """取角色册里的玩家角色（PlayerCharacter）列表。"""
    return [c for c in party.values() if isinstance(c, PlayerCharacter)]


def fold_combat_writeback(party: dict[str, Combatant], combat_state: dict) -> dict[str, Any]:
    """把战斗结束后的参战者状态（HP/存活）折回角色册，并汇总一份战斗结算摘要。

    战斗子图与会话主图共用同一批玩家角色对象（同一引用），HP 其实已就地更新；
    这里再显式对齐一次（容错：战斗内若换了对象），并产出 last_combat 摘要。
    """
    combatants = combat_state.get("combatants", {}) or {}
    for pc_id, pc in party.items():
        fighter = combatants.get(pc_id)
        if fighter is not None:
            pc.current_hp = fighter.current_hp      # 对齐 HP
            pc.life_state = fighter.life_state       # 对齐存活状态
            pc.conditions = fighter.conditions       # 对齐残留状态

    scene_ctx = combat_state.get("scene_context", {}) or {}
    casualties = [
        {"id": c.id, "name": c.name, "faction": str(c.faction.value)}
        for c in combatants.values() if not c.is_alive
    ]
    return {
        "outcome": _outcome_value(combat_state.get("outcome")),
        "granted_loot": scene_ctx.get("granted_loot"),
        "casualties": casualties,
    }


def _outcome_value(value: Any) -> str | None:
    """容忍 outcome 为枚举或字符串，取其字符串值。"""
    return getattr(value, "value", value)
