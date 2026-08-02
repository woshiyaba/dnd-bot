"""会话主图：把中央 DM 与战斗编排成一整局冒险。

落实 docs/DM/01-中央DM主图方案.md §2（选项 A：战斗作为子图嵌入）：

    START → dm_turn(DM 子图) → route_session ─┬─(wait)──► evaluate_advancement
                                              └─(combat)► resolve_engagement
                                                          → run_combat(战斗子图·包装节点)
                                                          → evaluate_advancement
             evaluate_advancement ─┬─(stay)────► final_narrate_turn → END
                                    └─(advance)► enter_beat → final_narrate_turn → END

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
from src.dm.tools import set_dice_provider
from src.model.combat_state import load_combatant
from src.model.combatant import Character
from src.model.dm_state import DMState, fold_combat_writeback
from src.session import action_nodes, story_nodes
from src.session.dm_subgraph import build_dm_subgraph, log_event

logger = logging.getLogger(__name__)

# 让 DM 的骰子工具（探索期暗骰）接到引擎当前可复现骰子上（session → dm 注入，方向合规）
set_dice_provider(current_engine_dice)


# ---------------------------------------------------------------------------
# 路由：DM 子图跑完后，看是继续等玩家、还是进战斗
# ---------------------------------------------------------------------------
def route_session(state: DMState) -> str:
    """读 DM 子图写下的 next 信号。"""
    if state.get("next") == "combat":
        return "combat"
    if state.get("next") == "action":
        return "action"
    return "wait"


def route_session_input(state: DMState) -> str:
    """结构化按钮行动绕过意图映射，其余输入进入 DM 决策子图。"""
    return "action" if state.get("structured_action") else "dm"


def resolve_engagement(state: DMState) -> dict:
    """提交开战前迁移，并把 DM 引用解析成完整、严格的遭遇请求。"""
    request = dict(state.get("combat_request") or {})
    if not request:
        raise ValueError("[session] start_combat 缺少 combat_request")

    working = dict(state)
    world_update = story_nodes.apply_world_writes(state)
    working.update(world_update)
    transition_to = (request.get("before_combat") or {}).get("transition_to_beat_id")
    transition_update: dict = {}
    if transition_to:
        transition_update = story_nodes.transition_to_beat(
            working,
            transition_to,
            reason=request.get("reason") or "玩家主动进入战斗",
        )
        working.update(transition_update)

    canon = story_nodes.current_canon(working)
    story = working.get("story") or {}
    scene = dict(working.get("scene") or {})
    encounter_id = request.get("encounter_id")

    if encounter_id:
        resolved = canon.encounter(encounter_id) if canon else None
        if resolved is None:
            raise ValueError(f"[session] 遭遇 «{encounter_id}» 不存在")
        encounter_beat, encounter = resolved
        if story.get("current_beat_id") != encounter_beat.id:
            raise ValueError(
                f"[session] 遭遇 «{encounter_id}» 不属于当前拍 "
                f"«{story.get('current_beat_id')}»"
            )
        monster_ids = list(encounter.monster_ids)
        request.update(
            {
                "encounter_id": encounter.id,
                "monster_ids": monster_ids,
                "target_actor_ids": monster_ids,
                "loot_table": list(encounter.loot_table),
                "xp_reward": encounter.xp_reward,
                "action_definitions": [
                    action.to_dict() for action in canon.action_definitions
                ],
            }
        )
        if encounter.random_seed is not None:
            request["random_seed"] = encounter.random_seed
        else:
            request.pop("random_seed", None)
    else:
        request["encounter_id"] = (
            f"ad_hoc:{story.get('current_beat_id', 'scene')}:"
            f"{story.get('turn_index', 0)}"
        )

    actors = {
        actor.get("actor_id"): actor
        for actor in scene.get("actors", [])
        if actor.get("actor_id")
    }
    targets = list(request.get("target_actor_ids") or request.get("monster_ids") or [])
    if not targets:
        raise ValueError("[session] 遭遇没有目标 actor")
    for actor_id in targets:
        actor = actors.get(actor_id)
        if actor is None:
            raise ValueError(f"[session] 当前场景不存在 actor «{actor_id}»")
        if not actor.get("card"):
            raise ValueError(f"[session] actor «{actor_id}» 缺少战斗卡面")
        actor["disposition"] = "hostile"
    scene["actors"] = list(actors.values())

    request["monster_ids"] = targets
    request["target_actor_ids"] = targets
    request["story_flags"] = dict(story.get("flags", {}))
    request["before_combat"] = {}

    return {
        **world_update,
        **transition_update,
        "scene": scene,
        "combat_request": request,
        "world_writes": None,
        "campaign_log": log_event(
            working,
            {
                "event": "engagement_resolved",
                "encounter_id": request["encounter_id"],
                "targets": targets,
            },
        ),
    }


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
        if not actor:
            raise ValueError(f"[run_combat] 当前场景不存在敌方 actor «{mid}»")
        if not actor.get("card"):
            raise ValueError(f"[run_combat] 敌方 actor «{mid}» 缺少战斗卡面")
        entry = {
            "type": actor.get("type", "monster"),
            "card": actor["card"],
            "faction": "enemy",
        }
        enemy = load_combatant(entry)
        combatants[enemy.id] = enemy

    if not party:
        raise ValueError("[run_combat] 战斗缺少玩家参战者")
    if len(combatants) <= len(party):
        raise ValueError("[run_combat] 战斗缺少合法对立方")

    random_seed = request.get("random_seed")
    if random_seed is None:
        random_seed = scene.get("random_seed")

    scene_context = {
        "surprised": request.get("surprised", []),
        "loot_table": request.get("loot_table", scene.get("loot_table", [])),
        "xp_reward": int(request.get("xp_reward", scene.get("xp_reward", 0))),
        "encounter_id": request.get("encounter_id"),
        "action_definitions": list(request.get("action_definitions", [])),
        "used_session_rule_actions": list(state.get("used_rule_actions", []) or []),
        "story_flags": dict(request.get("story_flags", {})),
        "surprise_context": {
            "reason": request.get("reason", ""),
            "dm_suggested_surprised": list(request.get("surprised", [])),
        },
        "reason": request.get("reason", ""),
        "location": scene.get("location"),
        "dm_mode": "llm",
    }
    if random_seed is not None:
        scene_context["random_seed"] = random_seed
    return combatants, scene_context


# 战斗子图：可嵌入（不挂 checkpointer），由会话主图统一驱动
_COMBAT_SUBGRAPH = build_combat_graph(embeddable=True)


async def run_combat(state: DMState) -> dict:
    """包装节点：进入战斗子图跑完一整场，结束后把结果折回世界。

    **纯而廉价**（每次恢复都会重跑本节点，但只做状态映射）；战斗中断冒泡到主图，
    战斗本身从自身检查点续跑。战斗结束后：把 HP/存活折回队伍、记录战利品/伤亡、
    从场景里清除已被击败的非玩家在场者，并登记关键 NPC 死亡。

    用 ``ainvoke`` 调战斗子图（其 DM 节点为 async），中断同样冒泡到会话主图。
    """
    combatants, scene_context = _build_combat_input(state)
    logger.info("[run_combat] 进入战斗 | 参战者=%d", len(combatants))

    combat_state = await _COMBAT_SUBGRAPH.ainvoke(
        {
            "combatants": combatants,
            "scene_context": scene_context,
        }
    )

    party = dict(state.get("party") or {})
    last_combat = fold_combat_writeback(party, combat_state)
    last_combat["encounter_id"] = scene_context.get("encounter_id")
    settled_scene = combat_state.get("scene_context", {}) or {}
    growth = dict(settled_scene.get("growth", {}) or {})
    last_combat["xp_reward"] = int(scene_context.get("xp_reward", 0))
    last_combat["growth"] = growth
    session_action_ids = {
        str(definition.get("id"))
        for definition in scene_context.get("action_definitions", [])
        if (definition.get("usage") or {}).get("kind") == "once_per_session"
    }
    used_rule_actions = list(state.get("used_rule_actions", []) or [])
    for action_id in combat_state.get("used_rule_actions", []) or []:
        if action_id in session_action_ids and action_id not in used_rule_actions:
            used_rule_actions.append(action_id)

    casualty_ids = {c["id"] for c in last_combat.get("casualties", [])}
    scene, story, death_events = _fold_world_casualties(state, casualty_ids)
    story, scene, party, last_combat, settlement_events = (
        story_nodes.settle_combat_victory(
            state,
            story=story,
            scene=scene,
            party=party,
            last_combat=last_combat,
        )
    )

    logger.info(
        "[run_combat] 战斗结束 | outcome=%s 伤亡=%d",
        last_combat.get("outcome"),
        len(casualty_ids),
    )
    messages = _append_combat_messages(
        state.get("messages", []), combat_state.get("combat_log", [])
    )
    for actor_id, summary in growth.items():
        gained = int(summary.get("experience_gained", 0))
        if gained <= 0:
            continue
        actor = party[actor_id]
        level_text = (
            f"，升至 {summary['new_level']} 级"
            if summary.get("new_level") != summary.get("old_level")
            else ""
        )
        unlock_count = len(summary.get("unlocked", []))
        unlock_text = f"，解锁 {unlock_count} 项能力" if unlock_count else ""
        messages.append(
            {
                "role": "system",
                "character_id": actor_id,
                "content": (
                    f"{actor.name} 获得 {gained} XP（累计 {summary['experience']}）"
                    f"{level_text}{unlock_text}。"
                ),
            }
        )
    campaign_log = log_event(state, {"event": "combat", **last_combat})
    for event in [*death_events, *settlement_events]:
        campaign_log = log_event({"campaign_log": campaign_log}, event)
    return {
        "party": party,
        "scene": scene,
        "story": story,
        "messages": messages,
        "last_combat": last_combat,
        "used_rule_actions": used_rule_actions,
        "combat_request": None,
        "next": "wait",
        "campaign_log": campaign_log,
    }


def _fold_world_casualties(
    state: DMState, casualty_ids: set[str]
) -> tuple[dict, dict, list[dict]]:
    """把非玩家伤亡写回世界，并记录关键 NPC 死亡供 DM 后续承接。"""
    party = state.get("party") or {}
    scene = dict(state.get("scene") or {})
    scene["actors"] = [
        actor
        for actor in scene.get("actors", [])
        if actor.get("actor_id") not in casualty_ids
    ]
    scene.pop("threat", None)  # 战斗已发生，清掉「潜在威胁」提示

    story = dict(state.get("story") or {})
    removed = list(story.get("removed_actor_ids", []))
    critical_deaths = list(story.get("critical_npc_deaths", []))
    death_events: list[dict] = []
    canon = story_nodes.current_canon(state)
    for actor_id in casualty_ids:
        if actor_id in party:
            continue
        if actor_id not in removed:
            removed.append(actor_id)
        spec = canon.npc(actor_id) if canon else None
        if spec is None or not spec.story_critical or actor_id in critical_deaths:
            continue
        critical_deaths.append(actor_id)
        death_events.append(
            {
                "event": "critical_npc_death",
                "actor_id": spec.id,
                "name": spec.name,
                "role": spec.role,
            }
        )
    story["removed_actor_ids"] = removed
    story["critical_npc_deaths"] = critical_deaths
    return scene, story, death_events


def route_after_combat(state: DMState) -> str:
    """属性提升未完成时先在图边界停下，完成后再接受下一次剧情命令。"""
    pending = any(
        isinstance(actor, Character) and actor.pending_ability_points > 0
        for actor in (state.get("party") or {}).values()
    )
    return "level_up" if pending else "continue"


def _append_combat_messages(messages: list[dict], combat_log: list[dict]) -> list[dict]:
    """把战斗声明与 DM 叙述折回会话历史，供刷新和战后继续对话。"""
    result = list(messages or [])
    for event in combat_log or []:
        if event.get("event") == "declaration" and event.get("text"):
            result.append(
                {
                    "role": "user",
                    "content": event["text"],
                    "character_id": event.get("actor_id") or event.get("actor"),
                }
            )
        elif event.get("event") in {"combat_opening", "narration"} and event.get(
            "text"
        ):
            result.append({"role": "dm", "content": event["text"]})
    return result


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def build_session_graph(checkpointer: Any | None = None):
    """构建并编译会话主图。

    checkpointer 缺省用带战斗模型白名单的 MemorySaver；多人/重启用持久化版（SQLite/MySQL）。
    """
    g = StateGraph(DMState)

    g.add_node("dm_turn", build_dm_subgraph())  # DM 子图（同 schema，直接嵌入）
    g.add_node("prepare_world_action", action_nodes.prepare_world_action)
    g.add_node("commit_world_action", action_nodes.commit_world_action)
    g.add_node("execute_world_action", action_nodes.execute_world_action)
    g.add_node("resolve_engagement", resolve_engagement)
    g.add_node("prepare_engagement_recap", story_nodes.prepare_engagement_act_recap)
    g.add_node("run_combat", run_combat)  # 战斗子图（包装节点映射 schema）
    # 故事推进段（糖葫芦串珠：触发推进 / 否则探索）
    g.add_node("evaluate_advancement", story_nodes.evaluate_advancement)
    g.add_node("enter_beat", story_nodes.enter_beat)
    g.add_node("final_narrate_turn", story_nodes.final_narrate_turn)
    g.add_node("epilogue", story_nodes.epilogue)

    g.add_conditional_edges(
        START,
        route_session_input,
        {"dm": "dm_turn", "action": "prepare_world_action"},
    )
    # DM 回合后：进战斗 → 战后叙述 → 推进判定；否则直接推进判定
    g.add_conditional_edges(
        "dm_turn",
        route_session,
        {
            "wait": "evaluate_advancement",
            "combat": "prepare_engagement_recap",
            "action": "prepare_world_action",
        },
    )
    g.add_edge("prepare_world_action", "commit_world_action")
    g.add_edge("commit_world_action", "execute_world_action")
    g.add_edge("execute_world_action", "evaluate_advancement")
    g.add_edge("prepare_engagement_recap", "resolve_engagement")
    g.add_edge("resolve_engagement", "run_combat")
    g.add_conditional_edges(
        "run_combat",
        route_after_combat,
        {"level_up": END, "continue": "evaluate_advancement"},
    )
    # 推进判定：命中切拍，否则把控制权交回玩家（END，等下一条消息）
    g.add_conditional_edges(
        "evaluate_advancement",
        story_nodes.route_advancement,
        {
            "advance": "enter_beat",
            "stay": "final_narrate_turn",
        },
    )
    g.add_edge("enter_beat", "final_narrate_turn")
    # 最终统一叙述后：新拍是结局拍则收尾，否则交回玩家
    g.add_conditional_edges(
        "final_narrate_turn",
        story_nodes.route_ending,
        {
            "ending": "epilogue",
            "ongoing": END,
        },
    )
    g.add_edge("epilogue", END)

    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver(serde=build_serde())

    return g.compile(checkpointer=checkpointer)


def reset_session_dice(scene_context: dict) -> None:
    """按场景 random_seed 重置引擎骰子（让探索期 DM 暗骰也可复现）。"""
    seed = scene_context.get("random_seed")
    if seed is not None:
        reset_engine_dice(int(seed))
