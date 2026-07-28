"""探索阶段统一规则行动的编译、成本提交与世界执行节点。"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.combat.action_compiler import prepare_action_plan
from src.combat.action_executor import (
    commit_action_cost,
    execute_world_plan,
    preflight_world_effects,
)
from src.combat.action_registry import world_action_entries
from src.model.combatant import Combatant
from src.model.dm_state import DMState
from src.session import story_nodes
from src.session.dm_subgraph import log_event

logger = logging.getLogger(__name__)


def available_world_actions(
    state: DMState, *, actor_id: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回当前角色可见的世界行动条目与定义索引。"""
    canon = story_nodes.current_canon(state)
    party = state.get("party") or {}
    selected_actor_id = actor_id or state.get("active_actor_id")
    actor = party.get(selected_actor_id)
    if canon is None or actor is None:
        return [], {}
    story = state.get("story") or {}
    definitions = [
        action for action in canon.action_definitions if "world" in action.scopes
    ]
    return world_action_entries(
        actor,
        party,
        canon_definitions=definitions,
        flags=dict(story.get("flags", {})),
        beat_id=story.get("current_beat_id"),
        location_id=story.get("current_location_id"),
        used_action_ids=list(state.get("used_rule_actions", []) or []),
    )


async def prepare_world_action(state: DMState) -> dict:
    """把按钮声明或 DM 映射结果编译为已校验的 v2 世界计划。"""
    declaration = state.get("structured_action") or {}
    actor = _action_actor(state)
    entries, definitions = available_world_actions(state, actor_id=actor.id)
    action_id = str(declaration.get("action_id") or "")
    entry = next(
        (
            item
            for item in entries
            if item.get("action_id") == action_id and item.get("enabled")
        ),
        None,
    )
    definition = definitions.get(action_id)
    if entry is None or definition is None:
        raise ValueError(f"世界规则行动 «{action_id}» 当前不可用")
    target_ids = list(dict.fromkeys(map(str, declaration.get("target_ids") or [])))
    if declaration.get("target_id") and not target_ids:
        target_ids = [str(declaration["target_id"])]
    legal_targets = {str(item["id"]) for item in entry.get("targets", [])}
    if not set(target_ids).issubset(legal_targets):
        raise ValueError("世界规则行动目标不合法")
    plan = await prepare_action_plan(
        definition=definition,
        actor=actor,
        targets=state.get("party") or {},
        selected_target_ids=target_ids,
        scope="world",
        context={
            "beat_id": (state.get("story") or {}).get("current_beat_id"),
            "location_id": (state.get("story") or {}).get("current_location_id"),
        },
    )
    preflight_world_effects(
        list(plan.get("effects", [])), actor, state.get("party") or {}
    )
    return {"pending_action_plan": plan}


def commit_world_action(state: DMState) -> dict:
    """提交世界行动成本；该节点不含中断，恢复时不会重复扣除。"""
    actor = _action_actor(state)
    plan = state.get("pending_action_plan")
    if not isinstance(plan, dict):
        raise ValueError("世界规则行动缺少已校验计划")
    used, committed, event = commit_action_cost(
        actor,
        plan,
        list(state.get("used_rule_actions", []) or []),
        list(state.get("committed_action_plans", []) or []),
    )
    return {
        "party": state.get("party") or {},
        "used_rule_actions": used,
        "committed_action_plans": committed,
        "action_events": [event],
        "campaign_log": log_event(state, event),
    }


def execute_world_action(state: DMState) -> dict:
    """执行世界计划并产出受限世界写入，随后交由故事节点统一提交。"""
    actor = _action_actor(state)
    plan = state.get("pending_action_plan")
    if not isinstance(plan, dict):
        raise ValueError("世界规则行动缺少已校验计划")
    events, writes = execute_world_plan(state, actor, plan)
    campaign_log = list(state.get("campaign_log", []) or [])
    for event in events:
        campaign_log = log_event({"campaign_log": campaign_log}, event)
    logger.info(
        "[execute_world_action] actor=%s action=%s effects=%d",
        actor.id,
        plan.get("definition_id"),
        len(events),
    )
    return {
        "party": state.get("party") or {},
        "world_writes": writes,
        "action_events": events,
        "pending_action_plan": None,
        "structured_action": None,
        "intent": "use_action",
        "next": "wait",
        "reply_brief": (
            "玩家的规则行动已经由引擎结算；必须准确承接这些结构事件："
            + json.dumps(events, ensure_ascii=False)
        ),
        "campaign_log": campaign_log,
    }


def _action_actor(state: DMState) -> Combatant:
    """读取当前发言者角色；世界行动不得替其他玩家提交。"""
    actor_id = state.get("active_actor_id")
    actor = (state.get("party") or {}).get(actor_id)
    if actor is None:
        raise ValueError("世界规则行动缺少当前玩家角色")
    return actor
