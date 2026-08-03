"""StoryPlan 候选中可唯一推导字段的确定性归一化。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.schemas.story import StoryDesignBrief, StoryPlanCandidate


def normalize_story_plan_candidate(
    raw: dict[str, Any], brief: StoryDesignBrief
) -> dict[str, Any]:
    """覆盖可唯一推导的字段，不改写数量、拓扑、ID 或剧情内容。"""
    if brief.scale_profile is None:
        raise ValueError("确认设计稿缺少 scale_profile")

    candidate = StoryPlanCandidate.model_validate(raw)
    normalized = deepcopy(candidate.model_dump())
    normalized["scale_profile"] = brief.scale_profile.model_dump()

    beats = normalized["beats"]
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
    derived_sections = ("scale_profile", "acts", "beats", "branch_points")
    return [name for name in derived_sections if before.get(name) != after.get(name)]
