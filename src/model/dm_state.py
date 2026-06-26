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

from src.model.canon import Beat, Canon
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

    # —— 故事进度（糖葫芦剧本骨架，见 src/model/canon.py）——
    campaign_id: str              # 本局 canon 的注册表 key（canon 本体不入 state，按引用存）
    story: dict                   # 进度工作区：current_beat_id/visited_beats/flags/delivered_clues/
                                  #   visited_locations/current_location_id/beat_entered_turn/idle_turns/
                                  #   turn_index/pending_next_beat_id（纯 dict，JSON 可序列化，规避 serde）
    world_writes: dict | None     # DM 本回合声明的世界写入：{flags_set, moved_to, clues_delivered}，引擎校验后消费
    next_story: str               # 推进路由信号：advance（切到下一拍）| stay（留在本拍）
    story_status: str             # ongoing | finished（结局拍叙述完置 finished）

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


# ---------------------------------------------------------------------------
# 故事系统：用一拍的 entry_state 搭起世界场景，并初始化整局进度
# ---------------------------------------------------------------------------
def build_beat_scene(canon: Canon, beat: Beat) -> dict:
    """把一拍的 ``entry_state`` 落成一份 WorldScene（DM 感知用，形状见本模块顶部约定）。

    地点名/描述缺省时由 ``LocationSpec`` 补全；若本拍有预置遭遇，则把战利品表/随机种子下放到 scene，
    供会话主图 ``run_combat`` 触发战斗时直接读取（无需改 run_combat）。
    """
    entry = dict(beat.entry_state or {})
    location_id = entry.get("location_id")
    scene: dict = {
        "beat_id": beat.id,                                  # 当前拍 id（便于前端/日志定位）
        "location_id": location_id,                          # 当前地点 id（location 触发器据此判定到达）
        "location": entry.get("location"),                   # 地点名
        "description": entry.get("description", ""),         # 环境描述
        "actors": [dict(a) for a in entry.get("actors", [])],  # 在场 NPC / 潜在敌人（带卡面）
        "exits": list(entry.get("exits", [])),               # 叙事出口提示
        "flags": dict(entry.get("flags", {})),               # 场景开关
        "threat": entry.get("threat"),                       # 威胁提示
        "dm_mode": entry.get("dm_mode"),                     # 由引擎在 init 时统一覆盖
    }
    # 用 LocationSpec 补全地点名/描述
    loc = canon.location(location_id) if location_id else None
    if loc is not None:
        scene["location"] = scene["location"] or loc.name
        if not scene["description"]:
            scene["description"] = loc.description
    # 预置遭遇参数下放到 scene（run_combat._build_combat_input 会读取）
    if beat.encounter is not None:
        if beat.encounter.loot_table:
            scene["loot_table"] = list(beat.encounter.loot_table)
        if beat.encounter.random_seed is not None:
            scene["random_seed"] = beat.encounter.random_seed
        scene["encounter_id"] = beat.encounter.id
    return scene


def init_story(canon: Canon) -> tuple[dict, dict]:
    """按起始拍初始化整局故事进度与初始世界场景。

    返回 ``(story, scene)``：``story`` 是进度工作区（纯 dict）；``scene`` 是起始拍搭好的 WorldScene。
    """
    start = canon.beat(canon.start_beat_id)
    if start is None:
        raise ValueError(f"canon «{canon.campaign_id}» 的 start_beat_id «{canon.start_beat_id}» 不存在")

    scene = build_beat_scene(canon, start)
    location_id = scene.get("location_id")
    story = {
        "current_beat_id": start.id,                                   # 当前在哪颗珠子
        "visited_beats": [start.id],                                   # 已走过的拍
        "flags": dict(scene.get("flags", {})),                         # 世界 flag（推进判定依据，唯一真相源）
        "delivered_clues": [],                                         # 已传达的关键线索 id
        "visited_locations": [location_id] if location_id else [],     # 已到达的地点 id
        "current_location_id": location_id,                            # 当前地点 id
        "beat_entered_turn": 0,                                        # 进入本拍时的回合序号
        "idle_turns": 0,                                              # 在本拍空转的回合数（驱动卡关兜底）
        "turn_index": 0,                                              # 全局回合计数
        "pending_next_beat_id": None,                                 # 待切入的下一拍（evaluate_advancement 命中时写）
    }
    return story, scene
