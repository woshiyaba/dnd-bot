"""StoryPlan 候选中可唯一推导字段的确定性归一化。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.schemas.story import StoryDesignBrief, StoryPlanCandidate


def normalize_story_plan_candidate(
    raw: dict[str, Any], brief: StoryDesignBrief
) -> dict[str, Any]:
    """编译可唯一推导字段与固定高潮出口，保留实体数量、ID 和剧情内容。"""
    if brief.scale_profile is None:
        raise ValueError("确认设计稿缺少 scale_profile")

    candidate = StoryPlanCandidate.model_validate(raw)
    normalized = deepcopy(candidate.model_dump())
    normalized["scale_profile"] = brief.scale_profile.model_dump()
    endings = normalized.pop("endings")
    if endings is not None:
        if any(beat["kind"] == "ending" for beat in normalized["beats"]):
            raise ValueError(
                "使用 endings 固定槽位时，beats 只能包含可玩节点，不得重复提供结局节点"
            )
        climaxes = [beat for beat in normalized["beats"] if beat["kind"] == "climax"]
        if len(climaxes) != 1:
            raise ValueError("编译固定结局需要恰好一个 climax 节点")
        climax = climaxes[0]
        minutes = max(
            1, round(brief.duration_minutes * brief.pacing.ending_percent / 100)
        )
        normalized["ending_routes"] = []
        for outcome in ("win", "lose"):
            ending_id = f"ending_{outcome}"
            content = endings[ending_id]
            normalized["beats"].append(
                {
                    "id": ending_id,
                    "kind": "ending",
                    "act_id": climax["act_id"],
                    "estimated_minutes": (
                        content["estimated_minutes"]
                        if content["estimated_minutes"] is not None
                        else minutes
                    ),
                    "objective": content["objective"],
                    "pressure": "",
                    "dramatic_question": "",
                    "entry_hook": "",
                    "location_ids": list(climax["location_ids"]),
                    "actor_ids": [],
                    "enemy_actor_ids": [],
                    "clue_ids": [],
                    "encounter_id": None,
                    "exits": [],
                    "fail_forward": "",
                    "payoff_flag_ids": [],
                }
            )
            normalized["ending_routes"].append(
                {
                    "ending_id": ending_id,
                    "outcome": outcome,
                    "required_facts": content["required_facts"],
                    "payoffs": content["payoffs"],
                }
            )
    if normalized["plan_version"] >= 4 and normalized.get("win_condition"):
        # 全局胜利与高潮出口表达同一条门槛，不要求模型重复抄写一遍图边。
        for beat in normalized["beats"]:
            if beat["kind"] == "climax":
                win = normalized["win_condition"]
                beat["exits"] = [
                    {
                        "to_beat_id": "ending_win",
                        "condition_summary": win["description"] or "达成全局胜利条件",
                        "consequence": "进入胜利结局",
                        "trigger": deepcopy(win),
                    }
                ]
                if not beat["fail_forward"] and normalized.get("lose_condition"):
                    beat["fail_forward"] = (
                        normalized["lose_condition"]["description"]
                        or "队伍战败时进入失败结局"
                    )

    beats = normalized["beats"]
    for beat in beats:
        if beat["kind"] == "ending":
            # 结局只需短暂收束；候选中的零分钟按 Canon 的最小计时单位编译。
            beat["estimated_minutes"] = max(1, beat["estimated_minutes"])
        for index, exit_ in enumerate(beat["exits"], start=1):
            if exit_.get("trigger") is not None:
                exit_["trigger"]["id"] = f"trigger_{beat['id']}_{index}"
    for name in ("win_condition", "lose_condition"):
        if normalized.get(name) is not None:
            normalized[name]["id"] = name
    beats_by_act: dict[str, list[dict[str, Any]]] = {}
    for beat in beats:
        beats_by_act.setdefault(beat["act_id"], []).append(beat)
    for act in normalized["acts"]:
        owned_beats = beats_by_act.get(act["id"], [])
        act["beat_ids"] = [beat["id"] for beat in owned_beats]
        act["estimated_minutes"] = sum(
            beat["estimated_minutes"] for beat in owned_beats
        )

    beats_by_id = {beat["id"]: beat for beat in beats}
    for branch in normalized["branch_points"]:
        source = beats_by_id.get(branch["beat_id"])
        if source is None:
            branch["choices"] = []
            continue
        branch["choices"] = list(
            dict.fromkeys(exit_["to_beat_id"] for exit_ in source["exits"])
        )

    payoff_flags: dict[str, list[str]] = {}
    for payoff in normalized["foreshadowing_payoffs"]:
        flags = payoff_flags.setdefault(payoff["payoff_beat_id"], [])
        if payoff["flag_id"] not in flags:
            flags.append(payoff["flag_id"])
    for beat in beats:
        beat["payoff_flag_ids"] = payoff_flags.get(beat["id"], [])

    return normalized


def story_plan_normalization_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    """返回脱敏的顶层变更摘要，供日志与指标记录。"""
    derived_sections = (
        "scale_profile",
        "acts",
        "beats",
        "branch_points",
        "ending_routes",
    )
    return [name for name in derived_sections if before.get(name) != after.get(name)]
