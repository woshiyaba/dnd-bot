"""会话主图的「故事推进」节点：触发推进 / 否则探索（糖葫芦串珠）。

落实 docs/故事框架/00-故事系统需求分析.md 第四节。铁律「**结构归引擎**」：拍与拍之间的推进只能由
引擎依据 canon 的推进条件判定，DM 无权跳拍。每个 DM 回合（或战斗结束）后，引擎做一件确定的事——
检查玩家这步有没有满足当前拍的某个推进条件：命中→切到出口指向的下一拍；未命中→留在原地继续探索。

节点：
- :func:`evaluate_advancement` —— 先消费 DM 声明的世界写入，再按推进条件判定（确定性优先，semantic 问 DM）。
- :func:`enter_beat` —— 用下一拍的 entry_state 搭好新场景。
- :func:`final_narrate_turn` —— 汇总本回合裁定、结算、切拍结果，生成唯一玩家可见叙述。
- :func:`epilogue` —— 结局拍叙述完，置整局 finished。

本模块属 ``session`` 层，可同时依赖 ``dm``（世界桥接）、``story``（canon 注册表）与 ``model``。
"""

from __future__ import annotations

import logging

from src.dm import world_bridge
from src.model.canon import (
    Canon,
    EndingOutcome,
    KeyInfo,
    Trigger,
    beat_brief,
    evaluate_trigger,
    managed_flag_sources,
)
from src.model.dm_state import DMState, build_beat_scene
from src.model.effects import InventoryItem
from src.session.common import llm_enabled, log_event
from src.story.loader import get_registry

logger = logging.getLogger(__name__)

# 空转多少回合后触发卡关兜底（在本拍没推进的连续回合数阈值）
STUCK_THRESHOLD = 3


# ---------------------------------------------------------------------------
# 取本局 canon / 当前拍骨架 / 卡关提示（供 dm_subgraph 与本模块共用）
# ---------------------------------------------------------------------------
def current_canon(state: DMState) -> Canon | None:
    """按 state 里的 campaign_id 从注册表取本局剧情圣经（无剧本则 None，退化为纯对话）。"""
    campaign_id = state.get("campaign_id")
    return get_registry().get(campaign_id) if campaign_id else None


def beat_brief_for(state: DMState) -> dict | None:
    """构造当前拍骨架画像，喂给 DM（无剧本/找不到拍则 None）。"""
    canon = current_canon(state)
    if canon is None:
        return None
    brief = beat_brief(canon, state.get("story") or {})
    if brief is not None:
        from src.session.action_nodes import available_world_actions

        actions, _ = available_world_actions(state)
        brief["available_actions"] = actions
    return brief


def stuck_hint_for(state: DMState) -> str | None:
    """空转超过阈值时，依据本拍 ``stuck_fallback`` 生成给 DM 的卡关兜底指令（否则 None）。"""
    canon = current_canon(state)
    story = state.get("story") or {}
    if canon is None or story.get("idle_turns", 0) < STUCK_THRESHOLD:
        return None
    beat = canon.beat(story.get("current_beat_id", ""))
    if beat is None:
        return None
    fb = beat.stuck_fallback or {}
    parts: list[str] = []
    critical_deaths = set(story.get("critical_npc_deaths", []))
    death_hints: list[str] = []
    for actor in beat.entry_state.get("actors", []):
        actor_id = actor.get("actor_id") or actor.get("npc_ref", "")
        if actor_id not in critical_deaths:
            continue
        spec = canon.npc(actor_id)
        if spec is not None and spec.death_fallback is not None:
            death_hints.append(
                spec.death_fallback.stuck_hint or spec.death_fallback.guidance
            )
    if death_hints:
        parts.extend(death_hints)
    elif fb.get("hint"):
        parts.append(str(fb["hint"]))
    if fb.get("reveal_clue"):
        delivered = set(story.get("delivered_clues", []))
        on_win_discoveries = set(
            beat.encounter.on_win_discoveries if beat.encounter is not None else []
        )
        undelivered = [
            k.text
            for k in beat.key_info
            if k.id not in delivered and k.id not in on_win_discoveries
        ]
        if undelivered:
            parts.append("主动抛出这条尚未传达的关键线索：" + undelivered[0])
    if fb.get("point_to_exit"):
        parts.append("并把玩家自然地指向出口：" + str(fb["point_to_exit"]))
    return " ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# 1) evaluate_advancement：触发推进 / 否则探索（引擎为主的确定性节点）
# ---------------------------------------------------------------------------
async def evaluate_advancement(state: DMState) -> dict:
    """检查玩家这步是否满足当前拍的推进条件，决定切拍还是留在原地。

    流程：消费 DM 声明的世界写入（+引擎自动写）→ 查全局胜负条件 → 查本拍推进条件
    （确定性 trigger 引擎判，semantic trigger 问 DM 一道是/否题）。命中置 ``next_story=advance``
    并记下 ``pending_next_beat_id``；未命中 ``idle_turns+=1`` 且 ``next_story=stay``。
    """
    canon = current_canon(state)
    story = dict(state.get("story") or {})
    if canon is None or not story:
        return {
            "next_story": "stay",
            "world_writes": None,
            "story_transition": {"type": "stay"},
        }  # 无剧本：退化为纯对话，不推进

    story["turn_index"] = story.get("turn_index", 0) + 1

    # 先把世界写入落进 story（DM 声明 + 引擎自动写），再据此判定推进
    story, scene, party, write_events = _apply_world_writes(canon, story, state)
    last_combat = _combat_result_with_discoveries(
        canon,
        story,
        party,
        state,
        state.get("last_combat"),
    )
    messages = state.get("messages", [])

    target_beat_id: str | None = None
    transition_reason = ""

    # 2) 已校验的显式行动迁移优先；它只能指向当前拍的 action 出口。
    explicit_target = story.get("pending_next_beat_id")
    if explicit_target:
        target_beat_id = explicit_target
        transition_reason = "DM 已确认的玩家行动"

    # 3) 全局胜负条件（达成即进对应结局拍）
    if (
        target_beat_id is None
        and canon.lose_condition is not None
        and await _condition_met(
            canon.lose_condition, story, scene, party, last_combat, messages, state
        )
    ):
        ending = canon.ending_beat(EndingOutcome.LOSE)
        target_beat_id = ending.id if ending else None
    elif (
        target_beat_id is None
        and canon.win_condition is not None
        and await _condition_met(
            canon.win_condition, story, scene, party, last_combat, messages, state
        )
    ):
        ending = canon.ending_beat(EndingOutcome.WIN)
        target_beat_id = ending.id if ending else None

    # 4) 本拍推进条件（确定性优先，semantic 问 DM）
    beat = canon.beat(story.get("current_beat_id", ""))
    if target_beat_id is None and beat is not None:
        for trig in beat.advance_conditions:
            if await _condition_met(
                trig, story, scene, party, last_combat, messages, state
            ):
                ex = beat.exit_for(trig.id)
                if ex is not None:
                    target_beat_id = ex.next_beat_id
                    transition_reason = trig.description or trig.id
                    logger.info(
                        "[evaluate_advancement] 命中触发器 «%s» → 切拍 «%s»",
                        trig.id,
                        target_beat_id,
                    )
                    break

    campaign_log = state.get("campaign_log", [])
    for ev in write_events:
        campaign_log = log_event({"campaign_log": campaign_log}, ev)

    if target_beat_id is not None:
        story["pending_next_beat_id"] = target_beat_id
        return {
            "story": story,
            "scene": scene,
            "party": party,
            "last_combat": last_combat,
            "next_story": "advance",
            "world_writes": None,
            "story_transition": {
                "type": "advance",
                "from_beat_id": story.get("current_beat_id"),
                "to_beat_id": target_beat_id,
                "reason": transition_reason,
            },
            "campaign_log": log_event(
                {"campaign_log": campaign_log},
                {"event": "advance", "next_beat_id": target_beat_id},
            ),
        }

    # 未命中：留在本拍。若这回合切实推动了世界（移动/获得线索/置 flag），不算空转并清零；
    # 否则空转 +1，连续空转够久才触发卡关兜底——避免「玩家正朝目标赶路」被误判成卡关而重发钩子。
    if write_events:
        story["idle_turns"] = 0
    else:
        story["idle_turns"] = story.get("idle_turns", 0) + 1
    logger.info(
        "[evaluate_advancement] 未推进，留在 «%s»（idle=%d，本回合世界写入=%d）",
        story.get("current_beat_id"),
        story["idle_turns"],
        len(write_events),
    )
    return {
        "story": story,
        "scene": scene,
        "party": party,
        "last_combat": last_combat,
        "next_story": "stay",
        "world_writes": None,
        "story_transition": {"type": "stay"},
        "campaign_log": campaign_log,
    }


async def _condition_met(
    trigger: Trigger,
    story: dict,
    scene: dict,
    party: dict,
    last_combat: dict | None,
    messages: list[dict],
    state: DMState,
) -> bool:
    """判定一个触发器是否满足：确定性的直接算，semantic 的问 DM 一道是/否题。"""
    verdict = evaluate_trigger(trigger, story, scene, party, last_combat)
    if verdict is not None:
        return verdict
    # semantic：引擎判不了 → 问 DM（窄判定），把玩家这步原话一并喂给 DM
    prompt = (trigger.predicate or {}).get("prompt") or trigger.description
    return await world_bridge.judge_trigger(
        prompt,
        scene,
        user_input=state.get("user_input"),
        messages=messages,
        use_llm=llm_enabled(state),
    )


def _apply_world_writes(
    canon: Canon, story: dict, state: DMState
) -> tuple[dict, dict, dict, list[dict]]:
    """提交 DM 世界写入与引擎自动写，返回 story、scene、party 和事件列表。

    - DM 写：``flags_set``（仅 canon ``declared_flags`` 白名单内）、``moved_to``（须在本拍地点内）、``clues_delivered``。
    - 引擎自动写：战斗胜利按本拍 ``encounter.on_win_flags`` 置 flag，并消费
      ``encounter.on_win_discoveries`` 中的敌人随身线索。
    """
    events: list[dict] = []
    declared = set(canon.declared_flags)
    writes = state.get("world_writes") or {}
    scene = dict(state.get("scene") or {})
    party = dict(state.get("party") or {})
    engine_managed_flags = set(managed_flag_sources(canon))

    flags = dict(story.get("flags", {}))
    for key, value in (writes.get("flags_set") or {}).items():
        if key not in declared:
            raise ValueError(f"[story] DM 尝试写入白名单外 flag «{key}»")
        if key in engine_managed_flags:
            raise ValueError(f"[story] 引擎管理 flag «{key}» 不能由 DM 直接写入")
        flags[key] = value
        events.append({"event": "flag_set", "flag": key, "value": value, "by": "dm"})

    beat = canon.beat(story.get("current_beat_id", ""))
    visited_locations = list(story.get("visited_locations", []))
    current_location_id = story.get("current_location_id")
    moved_to = writes.get("moved_to")
    if moved_to:
        if beat is None or moved_to not in beat.location_ids:
            raise ValueError(
                f"[story] 地点 «{moved_to}» 不属于当前拍 "
                f"«{story.get('current_beat_id')}»"
            )
        current_location_id = moved_to
        if moved_to not in visited_locations:
            visited_locations.append(moved_to)
        events.append({"event": "moved", "location_id": moved_to})
        rebuilt = build_beat_scene(
            canon,
            beat,
            location_id=moved_to,
            removed_actor_ids=story.get("removed_actor_ids", []),
        )
        rebuilt["dm_mode"] = scene.get("dm_mode")
        if scene.get("random_seed") is not None:
            rebuilt.setdefault("random_seed", scene.get("random_seed"))
        scene = rebuilt

    delivered = list(story.get("delivered_clues", []))
    current_clue_ids = {clue.id for clue in beat.key_info} if beat else set()
    on_win_discoveries = set(
        beat.encounter.on_win_discoveries
        if beat is not None and beat.encounter is not None
        else []
    )
    for clue_id in writes.get("clues_delivered", []):
        if clue_id not in current_clue_ids:
            raise ValueError(
                f"[story] 当前拍 «{story.get('current_beat_id')}» "
                f"不存在可传达线索 «{clue_id}»"
            )
        if clue_id in on_win_discoveries:
            raise ValueError(f"[story] 战后自动线索 «{clue_id}» 不能由 DM 提前传达")
        if clue_id not in delivered:
            delivered.append(clue_id)
            events.append({"event": "clue_delivered", "clue_id": clue_id})

    discovered = list(story.get("discovered_clues", []))
    if beat is not None:
        clue_by_id = {clue.id: clue for clue in beat.key_info}
        for clue_id in writes.get("discoveries", []):
            clue = clue_by_id.get(clue_id)
            if clue is None:
                raise ValueError(
                    f"[story] 当前拍 «{beat.id}» 不存在可发现线索 «{clue_id}»"
                )
            if clue_id in on_win_discoveries:
                raise ValueError(f"[story] 战后自动线索 «{clue_id}» 不能由 DM 提前发现")
            if clue_id in discovered:
                continue
            _apply_discovery_effects(
                clue,
                discovered=discovered,
                flags=flags,
                party=party,
                state=state,
                declared=declared,
                events=events,
                source="discovery",
            )

    transition_to = writes.get("transition_to_beat_id")
    if transition_to:
        if beat is None:
            raise ValueError("[story] 无当前剧情拍，不能执行跨拍行动")
        allowed = {
            ex.next_beat_id
            for ex in beat.exits
            if (
                trigger := next(
                    (
                        item
                        for item in beat.advance_conditions
                        if item.id == ex.trigger_id
                    ),
                    None,
                )
            )
            is not None
            and trigger.kind.value == "action"
        }
        if transition_to not in allowed:
            raise ValueError(
                f"[story] 当前拍 «{beat.id}» 不允许行动迁移到 «{transition_to}»"
            )
        story["pending_next_beat_id"] = transition_to
        events.append(
            {
                "event": "transition_requested",
                "from_beat_id": beat.id,
                "to_beat_id": transition_to,
            }
        )

    # 引擎自动写：战斗胜利 → on_win_flags + 敌人随身线索
    last_combat = state.get("last_combat") or {}
    if (
        last_combat.get("outcome") == "players_win"
        and beat is not None
        and beat.encounter is not None
        and last_combat.get("encounter_id") == beat.encounter.id
    ):
        for flag in beat.encounter.on_win_flags:
            if flag in declared and not flags.get(flag):
                flags[flag] = True
                events.append(
                    {"event": "flag_set", "flag": flag, "value": True, "by": "engine"}
                )
        clue_by_id = {clue.id: clue for clue in beat.key_info}
        for clue_id in beat.encounter.on_win_discoveries:
            if clue_id not in delivered:
                delivered.append(clue_id)
                events.append(
                    {
                        "event": "clue_delivered",
                        "clue_id": clue_id,
                        "by": "encounter_win",
                    }
                )
            if clue_id in discovered:
                continue
            _apply_discovery_effects(
                clue_by_id[clue_id],
                discovered=discovered,
                flags=flags,
                party=party,
                state=state,
                declared=declared,
                events=events,
                source="encounter_win",
            )

    story = {
        **story,
        "flags": flags,
        "visited_locations": visited_locations,
        "current_location_id": current_location_id,
        "delivered_clues": delivered,
        "discovered_clues": discovered,
    }
    return story, scene, party, events


def _apply_discovery_effects(
    clue: KeyInfo,
    *,
    discovered: list[str],
    flags: dict,
    party: dict,
    state: DMState,
    declared: set[str],
    events: list[dict],
    source: str,
) -> None:
    """原子提交一次新线索及其 flag、背包效果，并记录结构化事件。"""
    discovered.append(clue.id)
    effects = clue.discovery_effects or {}
    for key, value in (effects.get("flags_set") or {}).items():
        if key not in declared:
            raise ValueError(f"[story] 线索 «{clue.id}» 尝试写入未声明 flag «{key}»")
        flags[key] = value
        events.append(
            {
                "event": "flag_set",
                "flag": key,
                "value": value,
                "by": source,
                "clue_id": clue.id,
            }
        )
    for grant in effects.get("grant_items", []):
        recipient = _discovery_recipient(party, state, grant)
        item_id = str(grant.get("item_id") or "")
        quantity = int(grant.get("quantity", 1))
        if recipient is None or not item_id or quantity <= 0:
            raise ValueError(f"[story] 线索 «{clue.id}» 的物品发放配置无效")
        owned = next(
            (item for item in recipient.inventory if item.item_id == item_id),
            None,
        )
        if owned is None:
            recipient.inventory.append(
                InventoryItem(item_id=item_id, quantity=quantity)
            )
        else:
            owned.quantity += quantity
        events.append(
            {
                "event": "item_granted",
                "item_id": item_id,
                "quantity": quantity,
                "actor_id": recipient.id,
                "by": source,
                "clue_id": clue.id,
            }
        )
    events.append({"event": "clue_discovered", "clue_id": clue.id, "by": source})


def _combat_result_with_discoveries(
    canon: Canon,
    story: dict,
    party: dict,
    state: DMState,
    last_combat: dict | None,
) -> dict | None:
    """把胜利后自动取得的线索正文与物品事实加入本次战斗叙述上下文。"""
    if last_combat is None or last_combat.get("outcome") != "players_win":
        return last_combat
    beat = canon.beat(story.get("current_beat_id", ""))
    if (
        beat is None
        or beat.encounter is None
        or last_combat.get("encounter_id") != beat.encounter.id
        or not beat.encounter.on_win_discoveries
    ):
        return last_combat

    discoveries = []
    for clue_id in beat.encounter.on_win_discoveries:
        resolved = canon.clue(clue_id)
        if resolved is None:
            raise ValueError(f"[story] 战后自动线索 «{clue_id}» 不存在")
        _, clue = resolved
        granted_items = []
        for grant in (clue.discovery_effects or {}).get("grant_items", []):
            recipient = _discovery_recipient(party, state, grant)
            if recipient is None:
                raise ValueError(f"[story] 线索 «{clue_id}» 缺少物品接收角色")
            granted_items.append(
                {
                    "item_id": str(grant.get("item_id") or ""),
                    "quantity": int(grant.get("quantity", 1)),
                    "actor_id": recipient.id,
                    "actor_name": recipient.name,
                }
            )
        discoveries.append(
            {
                "id": clue.id,
                "text": clue.text,
                "granted_items": granted_items,
            }
        )
    return {**last_combat, "automatic_discoveries": discoveries}


def settle_combat_victory(
    state: DMState,
    *,
    story: dict,
    scene: dict,
    party: dict,
    last_combat: dict,
) -> tuple[dict, dict, dict, dict, list[dict]]:
    """在战斗节点返回前原子提交胜利 flag 与自动线索。

    返回更新后的 ``(story, scene, party, last_combat, events)``。即使战斗后需要先停下
    处理升级，线索与背包也已经完成结算；后续推进节点重复判定时会由 discovered id 幂等跳过。
    """
    canon = current_canon(state)
    if canon is None or not story:
        return story, scene, party, last_combat, []
    working = {
        **state,
        "story": story,
        "scene": scene,
        "party": party,
        "last_combat": last_combat,
        "world_writes": None,
    }
    story, scene, party, events = _apply_world_writes(canon, story, working)
    last_combat = _combat_result_with_discoveries(
        canon,
        story,
        party,
        working,
        last_combat,
    )
    return story, scene, party, last_combat, events


def _discovery_recipient(party: dict, state: DMState, grant: dict):
    """解析线索物品接收者；当前只允许发给实际发现线索的角色。"""
    if grant.get("recipient", "active_actor") != "active_actor":
        raise ValueError("[story] 只支持 recipient=active_actor")
    return party.get(state.get("active_actor_id"))


def apply_world_writes(state: DMState) -> dict:
    """立即提交本回合已校验的世界写入，供开战前原子准备阶段复用。"""
    canon = current_canon(state)
    story = dict(state.get("story") or {})
    if canon is None or not story:
        return {"world_writes": None}
    story, scene, party, events = _apply_world_writes(canon, story, state)
    campaign_log = list(state.get("campaign_log", []) or [])
    for event in events:
        campaign_log = log_event({"campaign_log": campaign_log}, event)
    return {
        "story": story,
        "scene": scene,
        "party": party,
        "campaign_log": campaign_log,
        "world_writes": None,
    }


# ---------------------------------------------------------------------------
# 2) enter_beat：用下一拍的 entry_state 搭好新场景
# ---------------------------------------------------------------------------
def enter_beat(state: DMState) -> dict:
    """切到 ``pending_next_beat_id`` 指向的下一拍：搭新 scene、更新进度、重置空转。"""
    story = state.get("story") or {}
    return transition_to_beat(
        state,
        story.get("pending_next_beat_id"),
        reason=(state.get("story_transition") or {}).get("reason"),
    )


def transition_to_beat(
    state: DMState, next_id: str | None, *, reason: str | None = None
) -> dict:
    """经上游校验后原子切换剧情拍，供普通推进与开战前迁移复用。"""
    canon = current_canon(state)
    story = dict(state.get("story") or {})
    from_beat_id = story.get("current_beat_id")
    previous_scene = dict(state.get("scene") or {})
    transition = dict(state.get("story_transition") or {"type": "advance"})
    beat = canon.beat(next_id) if canon else None
    if beat is None:  # 兜底：目标拍不存在 → 不切，留在原地
        raise ValueError(f"[enter_beat] 目标拍 «{next_id}» 不存在")

    if beat.entry_state.get("preserve_current_scene"):
        scene = dict(previous_scene)
        scene["beat_id"] = beat.id
        if beat.entry_state.get("description"):
            scene["description"] = beat.entry_state["description"]
    else:
        scene = build_beat_scene(
            canon,
            beat,
            removed_actor_ids=story.get("removed_actor_ids", []),
        )
    scene["dm_mode"] = previous_scene.get("dm_mode")  # 沿用本局 DM 模式
    if previous_scene.get("random_seed") is not None:
        scene.setdefault("random_seed", previous_scene["random_seed"])

    visited_beats = list(story.get("visited_beats", []))
    if beat.id not in visited_beats:
        visited_beats.append(beat.id)
    new_location = scene.get("location_id")
    visited_locations = list(story.get("visited_locations", []))
    if new_location and new_location not in visited_locations:
        visited_locations.append(new_location)
    # 合并新拍的初始 flags（保留已有世界 flag，新拍的作为补充）
    merged_flags = {**dict(scene.get("flags", {})), **story.get("flags", {})}

    story.update(
        {
            "current_beat_id": beat.id,
            "visited_beats": visited_beats,
            "current_location_id": new_location,
            "visited_locations": visited_locations,
            "flags": merged_flags,
            "idle_turns": 0,
            "beat_entered_turn": story.get("turn_index", 0),
            "pending_next_beat_id": None,
        }
    )
    logger.info("[enter_beat] 进入新拍 «%s»（%s）", beat.id, beat.title)
    transition.update(
        {
            "type": "advance",
            "from_beat_id": from_beat_id,
            "to_beat_id": beat.id,
            "to_beat_title": beat.title,
            "to_location": scene.get("location"),
            "reason": reason or transition.get("reason") or "玩家行动触发",
        }
    )
    return {
        "previous_scene": previous_scene,
        "scene": scene,
        "story": story,
        "story_transition": transition,
        "campaign_log": log_event(
            state, {"event": "enter_beat", "beat_id": beat.id, "title": beat.title}
        ),
    }


async def final_narrate_turn(state: DMState) -> dict:
    """统一生成本回合唯一玩家可见 DM 叙述。"""
    last_combat = (
        state.get("last_combat") if _current_turn_has_event(state, "combat") else None
    )
    text = await world_bridge.narrate_turn_final(
        user_input=state.get("user_input"),
        reply_brief=state.get("reply_brief"),
        narrative_intent=state.get("narrative_intent"),
        last_check=state.get("last_check"),
        last_combat=last_combat,
        previous_scene=state.get("previous_scene"),
        scene=state.get("scene") or {},
        beat_brief=beat_brief_for(state),
        story_transition=state.get("story_transition"),
        messages=state.get("messages"),
        use_llm=llm_enabled(state),
    )
    messages = list(state.get("messages", []))
    messages.append({"role": "dm", "content": text})
    return {
        "messages": messages,
        "campaign_log": log_event(state, {"event": "narration", "text": text}),
        "previous_scene": None,
        "story_transition": None,
    }


def _current_turn_has_event(state: DMState, event_type: str) -> bool:
    """检查上一条 narration 之后，本回合是否发生过指定结构事件。"""
    for event in reversed(state.get("campaign_log", []) or []):
        if event.get("event") == "narration":
            return False
        if event.get("event") == event_type:
            return True
    return False


# ---------------------------------------------------------------------------
# 3) narrate_beat：旧的独立切拍过场叙述 helper（主图当前不再直接使用）
# ---------------------------------------------------------------------------
async def narrate_beat(state: DMState) -> dict:
    """DM 叙述进入新一拍的过场（结局拍则叙述结局场景）。"""
    canon = current_canon(state)
    story = state.get("story") or {}
    scene = state.get("scene") or {}
    beat = canon.beat(story.get("current_beat_id", "")) if canon else None
    title = beat.title if beat else (scene.get("location") or "新的场景")

    text = await world_bridge.narrate_beat_transition(
        title, scene, use_llm=llm_enabled(state)
    )
    messages = list(state.get("messages", []))
    messages.append({"role": "dm", "content": text})
    return {
        "messages": messages,
        "campaign_log": log_event(state, {"event": "narration", "text": text}),
    }


# ---------------------------------------------------------------------------
# 4) epilogue：结局拍叙述完，整局 finished
# ---------------------------------------------------------------------------
def epilogue(state: DMState) -> dict:
    """到达结局拍并叙述完 → 置整局 ``story_status=finished``（可开新局）。"""
    story = state.get("story") or {}
    logger.info("[epilogue] 整局结束 | 结局拍=%s", story.get("current_beat_id"))
    return {
        "story_status": "finished",
        "campaign_log": log_event(
            state, {"event": "story_end", "beat_id": story.get("current_beat_id")}
        ),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def route_advancement(state: DMState) -> str:
    """evaluate_advancement 后：命中切拍还是留在原地。"""
    return "advance" if state.get("next_story") == "advance" else "stay"


def route_ending(state: DMState) -> str:
    """final_narrate_turn 后：新拍是结局拍则收尾，否则把控制权交回玩家。"""
    canon = current_canon(state)
    story = state.get("story") or {}
    beat = canon.beat(story.get("current_beat_id", "")) if canon else None
    return "ending" if (beat is not None and beat.is_ending) else "ongoing"
