"""会话主图的「故事推进」节点：触发推进 / 否则探索（糖葫芦串珠）。

落实 docs/故事框架/00-故事系统需求分析.md 第四节。铁律「**结构归引擎**」：拍与拍之间的推进只能由
引擎依据 canon 的推进条件判定，DM 无权跳拍。每个 DM 回合（或战斗结束）后，引擎做一件确定的事——
检查玩家这步有没有满足当前拍的某个推进条件：命中→切到出口指向的下一拍；未命中→留在原地继续探索。

节点：
- :func:`evaluate_advancement` —— 先消费 DM 声明的世界写入，再按推进条件判定（确定性优先，semantic 问 DM）。
- :func:`enter_beat` —— 用下一拍的 entry_state 搭好新场景。
- :func:`narrate_beat` —— DM 叙述「进入新珠子」的过场。
- :func:`epilogue` —— 结局拍叙述完，置整局 finished。

本模块属 ``session`` 层，可同时依赖 ``dm``（世界桥接）、``story``（canon 注册表）与 ``model``。
"""

from __future__ import annotations

import logging

from src.dm import world_bridge
from src.model.canon import (
    Canon,
    EndingOutcome,
    Trigger,
    beat_brief,
    evaluate_trigger,
)
from src.model.dm_state import DMState, build_beat_scene
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
    return beat_brief(canon, state.get("story") or {})


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
    if fb.get("hint"):
        parts.append(str(fb["hint"]))
    if fb.get("reveal_clue"):
        delivered = set(story.get("delivered_clues", []))
        undelivered = [k.text for k in beat.key_info if k.id not in delivered]
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
        }  # 无剧本：退化为纯对话，不推进

    story["turn_index"] = story.get("turn_index", 0) + 1

    # 先把世界写入落进 story（DM 声明 + 引擎自动写），再据此判定推进
    story, write_events = _apply_world_writes(canon, story, state)

    scene = state.get("scene") or {}
    party = state.get("party") or {}
    last_combat = state.get("last_combat")
    messages = state.get("messages", [])

    target_beat_id: str | None = None

    # 2) 全局胜负条件优先（达成即进对应结局拍）
    if canon.lose_condition is not None and await _condition_met(
        canon.lose_condition, story, scene, party, last_combat, messages, state
    ):
        ending = canon.ending_beat(EndingOutcome.LOSE)
        target_beat_id = ending.id if ending else None
    elif canon.win_condition is not None and await _condition_met(
        canon.win_condition, story, scene, party, last_combat, messages, state
    ):
        ending = canon.ending_beat(EndingOutcome.WIN)
        target_beat_id = ending.id if ending else None

    # 3) 本拍推进条件（确定性优先，semantic 问 DM）
    beat = canon.beat(story.get("current_beat_id", ""))
    if target_beat_id is None and beat is not None:
        for trig in beat.advance_conditions:
            if await _condition_met(
                trig, story, scene, party, last_combat, messages, state
            ):
                ex = beat.exit_for(trig.id)
                if ex is not None:
                    target_beat_id = ex.next_beat_id
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
            "next_story": "advance",
            "world_writes": None,
            "campaign_log": log_event(
                {"campaign_log": campaign_log},
                {"event": "advance", "next_beat_id": target_beat_id},
            ),
        }

    # 未命中：留在本拍，空转 +1
    story["idle_turns"] = story.get("idle_turns", 0) + 1
    logger.info(
        "[evaluate_advancement] 未推进，留在 «%s»（idle=%d）",
        story.get("current_beat_id"),
        story["idle_turns"],
    )
    return {
        "story": story,
        "next_story": "stay",
        "world_writes": None,
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
    # semantic：引擎判不了 → 问 DM（窄判定）
    prompt = (trigger.predicate or {}).get("prompt") or trigger.description
    return await world_bridge.judge_trigger(
        prompt, scene, messages=messages, use_llm=llm_enabled(state)
    )


def _apply_world_writes(
    canon: Canon, story: dict, state: DMState
) -> tuple[dict, list[dict]]:
    """把 DM 声明的世界写入（白名单校验）与引擎自动写落进 story，返回 ``(新 story, 事件列表)``。

    - DM 写：``flags_set``（仅 canon ``declared_flags`` 白名单内）、``moved_to``（须在本拍地点内）、``clues_delivered``。
    - 引擎自动写：战斗胜利按本拍 ``encounter.on_win_flags`` 置 flag（§4.5）。
    """
    events: list[dict] = []
    declared = set(canon.declared_flags)
    writes = state.get("world_writes") or {}

    flags = dict(story.get("flags", {}))
    for key, value in (writes.get("flags_set") or {}).items():
        if key in declared:
            flags[key] = value
            events.append(
                {"event": "flag_set", "flag": key, "value": value, "by": "dm"}
            )
        else:
            logger.warning("[story] 忽略白名单外的 flag «%s»（DM 越权声明）", key)

    beat = canon.beat(story.get("current_beat_id", ""))
    visited_locations = list(story.get("visited_locations", []))
    current_location_id = story.get("current_location_id")
    moved_to = writes.get("moved_to")
    if moved_to and beat is not None and moved_to in beat.location_ids:
        current_location_id = moved_to
        if moved_to not in visited_locations:
            visited_locations.append(moved_to)
        events.append({"event": "moved", "location_id": moved_to})

    delivered = list(story.get("delivered_clues", []))
    for clue_id in writes.get("clues_delivered", []):
        if clue_id not in delivered:
            delivered.append(clue_id)
            events.append({"event": "clue_delivered", "clue_id": clue_id})

    # 引擎自动写：战斗胜利 → on_win_flags
    last_combat = state.get("last_combat") or {}
    if (
        last_combat.get("outcome") == "players_win"
        and beat is not None
        and beat.encounter is not None
    ):
        for flag in beat.encounter.on_win_flags:
            if flag in declared and not flags.get(flag):
                flags[flag] = True
                events.append(
                    {"event": "flag_set", "flag": flag, "value": True, "by": "engine"}
                )

    story = {
        **story,
        "flags": flags,
        "visited_locations": visited_locations,
        "current_location_id": current_location_id,
        "delivered_clues": delivered,
    }
    return story, events


# ---------------------------------------------------------------------------
# 2) enter_beat：用下一拍的 entry_state 搭好新场景
# ---------------------------------------------------------------------------
def enter_beat(state: DMState) -> dict:
    """切到 ``pending_next_beat_id`` 指向的下一拍：搭新 scene、更新进度、重置空转。"""
    canon = current_canon(state)
    story = dict(state.get("story") or {})
    next_id = story.get("pending_next_beat_id")
    beat = canon.beat(next_id) if canon else None
    if beat is None:  # 兜底：目标拍不存在 → 不切，留在原地
        logger.warning("[enter_beat] 目标拍 «%s» 不存在，放弃切拍", next_id)
        story["pending_next_beat_id"] = None
        return {"story": story}

    scene = build_beat_scene(canon, beat)
    scene["dm_mode"] = (state.get("scene") or {}).get("dm_mode")  # 沿用本局 DM 模式

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
    return {
        "scene": scene,
        "story": story,
        "campaign_log": log_event(
            state, {"event": "enter_beat", "beat_id": beat.id, "title": beat.title}
        ),
    }


# ---------------------------------------------------------------------------
# 3) narrate_beat：DM 叙述「进入新珠子」的过场
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
    """enter_beat/narrate_beat 后：新拍是结局拍则收尾，否则把控制权交回玩家。"""
    canon = current_canon(state)
    story = state.get("story") or {}
    beat = canon.beat(story.get("current_beat_id", "")) if canon else None
    return "ending" if (beat is not None and beat.is_ending) else "ongoing"
