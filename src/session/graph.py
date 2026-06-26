"""会话主图：把中央 DM 与战斗编排成一整局冒险。

落实 docs/DM/01-中央DM主图方案.md §2（选项 A：战斗作为子图嵌入）：

    START → dm_turn(DM 子图) → route_session ─┬─(wait)──► END（等玩家下一条消息）
                                              └─(combat)► run_combat(战斗子图·包装节点)
                                                          → narrate_aftermath(DM) → END

子图嵌入方式（已在方案 §二经探针验证）：
- **DM 子图**与会话主图同为 ``DMState`` schema，直接 ``add_node(编译子图)``，中断（玩家明检定）
  天然冒泡到主图、恢复也由主图统一驱动。
- **战斗子图**是不同 schema（``CombatState``），用包装节点 ``run_combat`` 调
  ``combat_subgraph.invoke(...)``：进战斗前把队伍+遭遇组装成参战者，战斗结束后把 HP/战利品折回世界。
  战斗内部的攻击/先攻/豁免中断同样冒泡到主图。包装节点会在每次恢复时重跑，但它**纯而廉价**
  （只做状态映射），战斗本身从自己的检查点续跑、不重复结算（探针 3 已验证）。

主图持有唯一 checkpointer（serde 白名单复用战斗那份，因 ``DMState`` 同样持久化战斗模型对象）。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.combat.dice import current_engine_dice, reset_engine_dice
from src.combat.graph import build_combat_graph, build_serde
from src.dm import world_bridge
from src.dm.tools import set_dice_provider
from src.model.combat_state import load_combatant
from src.model.dm_state import DMState, fold_combat_writeback
from src.session.dm_subgraph import build_dm_subgraph, llm_enabled, log_event

logger = logging.getLogger(__name__)

# 让 DM 的骰子工具（探索期暗骰）接到引擎当前可复现骰子上（session → dm 注入，方向合规）
set_dice_provider(current_engine_dice)


# ---------------------------------------------------------------------------
# 路由：DM 子图跑完后，看是继续等玩家、还是进战斗
# ---------------------------------------------------------------------------
def route_session(state: DMState) -> str:
    """读 DM 子图写下的 next 信号。"""
    return "combat" if state.get("next") == "combat" else "wait"


# ---------------------------------------------------------------------------
# run_combat：包装节点——把世界状态映射进战斗子图，跑完再折回世界
# ---------------------------------------------------------------------------
def _build_combat_input(state: DMState) -> tuple[dict, dict]:
    """把「队伍 + 遭遇」组装成战斗子图的输入。

    返回 (combatants 字典, scene_context)。队伍角色对象直接复用（HP 延续），
    敌方从场景在场者的卡面构造。enter_combat 见到已给的 combatants 就不再重新加载。
    """
    request = state.get("combat_request") or {}
    scene = state.get("scene") or {}
    party = dict(state.get("party") or {})

    # 敌方：按 monster_ids 从场景在场者卡面构造
    actors = {a.get("actor_id"): a for a in scene.get("actors", [])}
    combatants = dict(party)  # 先放玩家方（同一引用，HP 延续）
    for mid in request.get("monster_ids", []):
        actor = actors.get(mid)
        if not actor or not actor.get("card"):
            continue
        entry = {"type": actor.get("type", "monster"), "card": actor["card"], "faction": "enemy"}
        enemy = load_combatant(entry)
        combatants[enemy.id] = enemy

    scene_context = {
        "random_seed": request.get("random_seed", scene.get("random_seed")),
        "surprised": request.get("surprised", []),
        "loot_table": request.get("loot_table", scene.get("loot_table", [])),
        "dm_mode": scene.get("dm_mode", "heuristic"),
    }
    return combatants, scene_context


# 战斗子图：可嵌入（不挂 checkpointer），由会话主图统一驱动
_COMBAT_SUBGRAPH = build_combat_graph(embeddable=True)


async def run_combat(state: DMState) -> dict:
    """包装节点：进入战斗子图跑完一整场，结束后把结果折回世界。

    **纯而廉价**（每次恢复都会重跑本节点，但只做状态映射）；战斗中断冒泡到主图，
    战斗本身从自身检查点续跑。战斗结束后：把 HP/存活折回队伍、记录战利品/伤亡、
    从场景里清除已被击败的敌意在场者。

    用 ``ainvoke`` 调战斗子图（其 DM 节点为 async），中断同样冒泡到会话主图。
    """
    combatants, scene_context = _build_combat_input(state)
    logger.info("[run_combat] 进入战斗 | 参战者=%d", len(combatants))

    combat_state = await _COMBAT_SUBGRAPH.ainvoke({
        "combatants": combatants,
        "scene_context": scene_context,
    })

    party = dict(state.get("party") or {})
    last_combat = fold_combat_writeback(party, combat_state)

    # 从场景里移除已被击败的敌人（保持世界一致）
    casualty_ids = {c["id"] for c in last_combat.get("casualties", [])}
    scene = dict(state.get("scene") or {})
    scene["actors"] = [
        a for a in scene.get("actors", [])
        if a.get("actor_id") not in casualty_ids
    ]
    scene.pop("threat", None)  # 战斗已发生，清掉「潜在威胁」提示

    logger.info("[run_combat] 战斗结束 | outcome=%s 伤亡=%d", last_combat.get("outcome"), len(casualty_ids))
    return {
        "party": party,
        "scene": scene,
        "last_combat": last_combat,
        "combat_request": None,
        "next": "wait",
        "campaign_log": log_event(state, {"event": "combat", **last_combat}),
    }


# ---------------------------------------------------------------------------
# narrate_aftermath：把故事交回 DM
# ---------------------------------------------------------------------------
async def narrate_aftermath(state: DMState) -> dict:
    """战斗结束后，DM 叙述战后世界并邀请玩家继续。"""
    text = await world_bridge.narrate_aftermath(
        state.get("last_combat") or {}, state.get("scene") or {}, use_llm=llm_enabled(state),
    )
    messages = list(state.get("messages", []))
    messages.append({"role": "dm", "content": text})
    return {"messages": messages, "campaign_log": log_event(state, {"event": "narration", "text": text})}


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_session_graph(checkpointer: Any | None = None):
    """构建并编译会话主图。

    checkpointer 缺省用带战斗模型白名单的 MemorySaver；多人/重启用持久化版（SQLite/MySQL）。
    """
    g = StateGraph(DMState)

    g.add_node("dm_turn", build_dm_subgraph())   # DM 子图（同 schema，直接嵌入）
    g.add_node("run_combat", run_combat)         # 战斗子图（包装节点映射 schema）
    g.add_node("narrate_aftermath", narrate_aftermath)

    g.add_edge(START, "dm_turn")
    g.add_conditional_edges("dm_turn", route_session, {
        "wait": END,
        "combat": "run_combat",
    })
    g.add_edge("run_combat", "narrate_aftermath")
    g.add_edge("narrate_aftermath", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver(serde=build_serde())

    return g.compile(checkpointer=checkpointer)


def reset_session_dice(scene_context: dict) -> None:
    """按场景 random_seed 重置引擎骰子（让探索期 DM 暗骰也可复现）。"""
    seed = scene_context.get("random_seed")
    if seed is not None:
        reset_engine_dice(int(seed))
