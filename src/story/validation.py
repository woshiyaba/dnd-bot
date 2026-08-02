"""StoryPlan、长篇 Canon 与运行时兼容性的确定性校验。"""

from __future__ import annotations

from collections import deque
from typing import Any

from src.combat.dice import parse_dice
from src.model.canon import (
    Beat,
    BeatKind,
    Canon,
    EndingOutcome,
    LocationSpec,
    NpcSpec,
    Trigger,
    TriggerKind,
    managed_flag_sources,
)
from src.model.combat_state import load_combatant
from src.model.rule_action import ActionDefinition
from src.schemas.story import (
    StoryDesignBrief,
    StoryPlan,
    StoryQualityMetrics,
    infer_length_mode,
    length_limits,
)


def story_plan_id_registry(plan: StoryPlan) -> dict[str, list[str]]:
    """只从已验证计划派生不可变 ID，不信任分片或模型自报 registry。"""
    registry = {
        "acts": [item.id for item in plan.acts],
        "beats": [item.id for item in plan.beats],
        "actors": [item.id for item in plan.entities.actors],
        "locations": [item.id for item in plan.entities.locations],
        "encounters": [item.id for item in plan.entities.encounters],
        "clues": [item.id for item in plan.entities.clues],
        "flags": [item.id for item in plan.entities.flags],
        "items": [item.id for item in plan.entities.items],
        "actions": [item.id for item in plan.entities.actions],
        "triggers": [
            f"trigger_{beat.id}_{index + 1}"
            for beat in plan.beats
            for index, _ in enumerate(beat.exits)
        ]
        + ["win_condition", "lose_condition"],
    }
    return {key: sorted(values) for key, values in registry.items()}


def validate_story_plan(plan: StoryPlan, brief: StoryDesignBrief) -> list[str]:
    """验证计划数量、DAG、可达性、汇流、路径时长、回收与 owner。"""
    errors: list[str] = []
    if brief.length_mode is None or brief.duration_minutes is None:
        return ["确认设计稿缺少 length_mode 或 duration_minutes"]
    if brief.scale_profile is None or brief.branching_budget is None:
        return ["确认设计稿缺少 scale_profile 或 branching_budget"]

    if plan.scale_profile != brief.scale_profile:
        errors.append("StoryPlan.scale_profile 必须与确认设计稿完全一致")

    limits = length_limits(brief.length_mode)
    playable = [beat for beat in plan.beats if beat.kind != "ending"]
    endings = [beat for beat in plan.beats if beat.kind == "ending"]
    counts = {
        "playable_beats": len(playable),
        "acts": len(plan.acts),
        "locations": len(plan.entities.locations),
        "encounters": len(plan.entities.encounters),
        "clues": len(plan.entities.clues),
    }
    targets = brief.scale_profile.model_dump()
    for name, value in counts.items():
        if value != targets[name]:
            errors.append(
                f"StoryPlan {name} 数量为 {value}，必须等于确认目标 {targets[name]}"
            )
        lower, upper = limits[name]
        if not lower <= value <= upper:
            errors.append(f"StoryPlan {name} 数量必须在 {lower} 到 {upper} 之间")

    registry = story_plan_id_registry(plan)
    for category, values in registry.items():
        duplicates = sorted({value for value in values if values.count(value) > 1})
        for value in duplicates:
            errors.append(f"StoryPlan {category} id «{value}» 重复")
    all_global = [
        value
        for category, values in registry.items()
        if category != "triggers"
        for value in values
    ]
    for value in sorted({value for value in all_global if all_global.count(value) > 1}):
        errors.append(f"StoryPlan 全局 id «{value}» 跨类别重复")

    beat_by_id = {beat.id: beat for beat in plan.beats}
    act_by_id = {act.id: act for act in plan.acts}
    if plan.start_beat_id not in beat_by_id:
        errors.append("StoryPlan.start_beat_id 不存在")

    membership: dict[str, list[str]] = {}
    for act in plan.acts:
        for beat_id in act.beat_ids:
            membership.setdefault(beat_id, []).append(act.id)
            if beat_id not in beat_by_id:
                errors.append(f"Act «{act.id}» 引用了不存在的 Beat «{beat_id}»")
        actual_minutes = sum(
            beat_by_id[beat_id].estimated_minutes
            for beat_id in act.beat_ids
            if beat_id in beat_by_id
        )
        if actual_minutes != act.estimated_minutes:
            errors.append(
                f"Act «{act.id}» 预计 {act.estimated_minutes} 分钟，"
                f"但所属 Beat 合计 {actual_minutes} 分钟"
            )
    for beat in plan.beats:
        if beat.act_id not in act_by_id:
            errors.append(f"Beat «{beat.id}» 引用了不存在的 Act «{beat.act_id}»")
        owners = membership.get(beat.id, [])
        if owners != [beat.act_id]:
            errors.append(f"Beat «{beat.id}» 必须且只能属于其 act_id 指定的 Act")

    location_ids = set(registry["locations"])
    actor_ids = set(registry["actors"])
    clue_ids = set(registry["clues"])
    encounter_ids = set(registry["encounters"])
    flag_ids = set(registry["flags"])
    for beat in plan.beats:
        for label, values, allowed in (
            ("location", beat.location_ids, location_ids),
            ("actor", beat.actor_ids, actor_ids),
            ("clue", beat.clue_ids, clue_ids),
            ("payoff flag", beat.payoff_flag_ids, flag_ids),
        ):
            for value in values:
                if value not in allowed:
                    errors.append(f"Beat «{beat.id}» 引用了不存在的 {label} «{value}»")
        if beat.encounter_id and beat.encounter_id not in encounter_ids:
            errors.append(
                f"Beat «{beat.id}» 引用了不存在的 encounter «{beat.encounter_id}»"
            )
        if beat.encounter_id and beat.kind == "ending":
            errors.append(f"结局 Beat «{beat.id}» 不能包含 Encounter")
        if beat.kind != "ending" and not beat.exits:
            errors.append(f"非结局 Beat «{beat.id}» 没有出口")
        if beat.kind == "ending" and beat.exits:
            errors.append(f"结局 Beat «{beat.id}» 不能有出口")
        for exit_ in beat.exits:
            if exit_.to_beat_id not in beat_by_id:
                errors.append(
                    f"Beat «{beat.id}» 指向不存在的 Beat «{exit_.to_beat_id}»"
                )

    # 分片按 Beat 编译，因此 clue/encounter 必须有且只有一个定义 owner；地点和角色至少被使用。
    clue_uses = [clue_id for beat in plan.beats for clue_id in beat.clue_ids]
    encounter_uses = [
        beat.encounter_id for beat in plan.beats if beat.encounter_id is not None
    ]
    for clue_id in clue_ids:
        if clue_uses.count(clue_id) != 1:
            errors.append(f"线索 «{clue_id}» 必须且只能属于一个 Beat")
    for encounter_id in encounter_ids:
        if encounter_uses.count(encounter_id) != 1:
            errors.append(f"Encounter «{encounter_id}» 必须且只能属于一个 Beat")
    used_locations = {value for beat in plan.beats for value in beat.location_ids}
    used_actors = {value for beat in plan.beats for value in beat.actor_ids}
    for location_id in sorted(location_ids - used_locations):
        errors.append(f"地点 «{location_id}» 没有被任何 Beat 使用")
    for actor_id in sorted(actor_ids - used_actors):
        errors.append(f"角色 «{actor_id}» 没有被任何 Beat 使用")

    adjacency = {
        beat.id: [
            exit_.to_beat_id for exit_ in beat.exits if exit_.to_beat_id in beat_by_id
        ]
        for beat in plan.beats
    }
    cycle = _find_cycle(adjacency)
    if cycle:
        errors.append("StoryPlan Beat 图必须是 DAG，检测到环：" + " → ".join(cycle))

    reachable = _reachable(plan.start_beat_id, adjacency)
    for beat in playable:
        if beat.id not in reachable:
            errors.append(f"非结局 Beat «{beat.id}» 从起点不可达")
    ending_ids = {beat.id for beat in endings}
    reverse = _reverse_graph(adjacency)
    can_finish: set[str] = set()
    for ending_id in ending_ids:
        can_finish.update(_reachable(ending_id, reverse))
    for beat in playable:
        if beat.id not in can_finish:
            errors.append(f"非结局 Beat «{beat.id}» 不存在通往结局的路径")

    outcomes = [route.outcome for route in plan.ending_routes]
    if outcomes.count("win") != 1 or outcomes.count("lose") != 1:
        errors.append("StoryPlan 必须恰好包含一条 win 和一条 lose 结局路线")
    route_ids = {route.ending_id for route in plan.ending_routes}
    if route_ids != ending_ids:
        errors.append("ending_routes 必须与两个 ending Beat 一一对应")

    required_branches = brief.branching_budget.meaningful_branch_points
    if len(plan.branch_points) != required_branches:
        errors.append(
            f"StoryPlan 分支数为 {len(plan.branch_points)}，必须等于确认预算 {required_branches}"
        )
    actual_branch_ids = {
        beat.id
        for beat in playable
        if len({exit_.to_beat_id for exit_ in beat.exits}) > 1
    }
    declared_branch_ids = {branch.beat_id for branch in plan.branch_points}
    if actual_branch_ids != declared_branch_ids:
        errors.append("branch_points 必须与所有多目标 Beat 一一对应")
    for branch in plan.branch_points:
        source = beat_by_id.get(branch.beat_id)
        if source is None:
            errors.append(f"分支引用不存在的源 Beat «{branch.beat_id}»")
            continue
        actual_choices = {exit_.to_beat_id for exit_ in source.exits}
        if set(branch.choices) != actual_choices:
            errors.append(f"分支 «{branch.beat_id}» 的 choices 必须等于源 Beat 出口")
        if len(branch.choices) > brief.branching_budget.max_parallel_beats:
            errors.append(f"分支 «{branch.beat_id}» 超过 max_parallel_beats")
        if len(branch.distinct_consequences) != len(branch.choices):
            errors.append(f"分支 «{branch.beat_id}» 的每个选择必须有独立后果")
        if branch.reconverge_at not in beat_by_id:
            errors.append(
                f"分支 «{branch.beat_id}» 的汇流点 «{branch.reconverge_at}» 不存在"
            )
            continue
        if brief.branching_budget.reconverge_before_climax and beat_by_id[
            branch.reconverge_at
        ].kind in {"climax", "ending"}:
            errors.append(f"分支 «{branch.beat_id}» 必须在高潮前汇流")
        for choice in branch.choices:
            distance = _shortest_distance(choice, branch.reconverge_at, adjacency)
            if distance is None or not 1 <= distance <= 2:
                errors.append(
                    f"分支 «{branch.beat_id}» 必须在 1–2 Beat 后汇流到 "
                    f"«{branch.reconverge_at}»"
                )

    clue_graph_ids = [item.clue_id for item in plan.clue_graph]
    if set(clue_graph_ids) != clue_ids or len(clue_graph_ids) != len(
        set(clue_graph_ids)
    ):
        errors.append("clue_graph 必须为每个 clue 提供且只提供一条记录")
    for clue in plan.clue_graph:
        if len(clue.alternative_approaches) < 2:
            errors.append(f"线索 «{clue.clue_id}» 至少需要两种可执行接近方式")

    topological = _topological_order(adjacency)
    order_index = {beat_id: index for index, beat_id in enumerate(topological)}
    for payoff in plan.foreshadowing_payoffs:
        if payoff.flag_id not in flag_ids:
            errors.append(f"伏笔回收引用不存在的 flag «{payoff.flag_id}»")
        if (
            payoff.setup_beat_id not in beat_by_id
            or payoff.payoff_beat_id not in beat_by_id
        ):
            errors.append(f"伏笔 «{payoff.flag_id}» 引用了不存在的 Beat")
        elif order_index.get(payoff.setup_beat_id, 10**9) >= order_index.get(
            payoff.payoff_beat_id, -1
        ):
            errors.append(f"伏笔 «{payoff.flag_id}» 的回收必须晚于铺垫")
        elif payoff.flag_id not in beat_by_id[payoff.payoff_beat_id].payoff_flag_ids:
            errors.append(
                f"伏笔 «{payoff.flag_id}» 未登记到回收 Beat 的 payoff_flag_ids"
            )
    registered_payoffs = {item.flag_id for item in plan.foreshadowing_payoffs}
    for beat in plan.beats:
        for flag_id in beat.payoff_flag_ids:
            if flag_id not in registered_payoffs:
                errors.append(
                    f"Beat «{beat.id}» 的 payoff flag «{flag_id}» 缺少伏笔记录"
                )

    owner_keys: set[tuple[str, str]] = set()
    valid_owner_ids = {
        "discovery": clue_ids,
        "encounter_win": encounter_ids,
        "initial_state": set(registry["beats"]),
        "rule_action": set(registry["actions"]),
        "dm_free_write": flag_ids,
    }
    for owner in plan.effect_owner_ledger:
        key = (owner.effect_kind, owner.effect_id)
        if key in owner_keys:
            errors.append(f"效果 «{owner.effect_id}» 存在多个 owner")
        owner_keys.add(key)
        expected_effects = (
            flag_ids if owner.effect_kind == "flag" else set(registry["items"])
        )
        if owner.effect_id not in expected_effects:
            errors.append(f"owner ledger 引用了不存在的效果 «{owner.effect_id}»")
        if owner.owner_id not in valid_owner_ids[owner.owner_kind]:
            errors.append(
                f"效果 «{owner.effect_id}» 的 owner_id «{owner.owner_id}» 与 owner_kind 不匹配"
            )
    expected_owner_keys = {
        *(("flag", value) for value in flag_ids),
        *(("item", value) for value in registry["items"]),
    }
    missing_owners = sorted(expected_owner_keys - owner_keys)
    for kind, value in missing_owners:
        errors.append(f"{kind} «{value}» 缺少唯一 owner")

    if not cycle and plan.start_beat_id in beat_by_id:
        beat_paths = _beat_paths(plan.start_beat_id, adjacency, ending_ids)
        path_minutes = _path_minutes(
            plan.start_beat_id,
            adjacency,
            {beat.id: beat.estimated_minutes for beat in plan.beats},
            ending_ids,
        )
        if not path_minutes:
            errors.append("StoryPlan 没有从起点到结局的完整路径")
        else:
            lower = max(limits["duration"][0], int(brief.duration_minutes * 0.8))
            upper = min(limits["duration"][1], int(brief.duration_minutes * 1.2))
            shortest, longest = min(path_minutes), max(path_minutes)
            if shortest < lower or shortest > upper:
                errors.append(f"最短路径 {shortest} 分钟不在允许区间 {lower}–{upper}")
            if longest < lower or longest > upper:
                errors.append(f"最长路径 {longest} 分钟不在允许区间 {lower}–{upper}")
        pacing_targets = {
            "opening": brief.pacing.opening_percent,
            "exploration_social": brief.pacing.exploration_social_percent,
            "escalation": brief.pacing.escalation_percent,
            "climax": brief.pacing.climax_percent,
            "ending": brief.pacing.ending_percent,
        }
        kind_bucket = {
            "opening": "opening",
            "exploration": "exploration_social",
            "conflict": "escalation",
            "climax": "climax",
            "ending": "ending",
        }
        for path_index, path in enumerate(beat_paths, start=1):
            total = sum(beat_by_id[beat_id].estimated_minutes for beat_id in path)
            if total <= 0:
                continue
            actual = dict.fromkeys(pacing_targets, 0)
            for beat_id in path:
                beat = beat_by_id[beat_id]
                actual[kind_bucket[beat.kind]] += beat.estimated_minutes
            for bucket, target in pacing_targets.items():
                percent = round(actual[bucket] * 100 / total)
                if abs(percent - target) > 15:
                    errors.append(
                        f"路径 {path_index} 的 {bucket} 节奏为 {percent}%，"
                        f"与确认目标 {target}% 偏差超过 15 个百分点"
                    )
    return errors


def validate_fragment_ids(
    fragment_kind: str, fragment: dict[str, Any], registry: dict[str, list[str]]
) -> list[str]:
    """拒绝分片创建计划外的全局 ID。"""
    errors: list[str] = []
    checks: list[tuple[str, list[str]]] = []

    def add_trigger_references(triggers: list[dict[str, Any]]) -> None:
        for trigger in triggers:
            predicate = trigger.get("predicate") or {}
            kind = trigger.get("kind")
            if kind == "flag":
                values = [
                    *predicate.get("all", []),
                    *predicate.get("any", []),
                ]
                if predicate.get("flag") is not None:
                    values.append(predicate["flag"])
                checks.append(("flags", [str(value) for value in values]))
            elif kind == "item" and predicate.get("item_id") is not None:
                checks.append(("items", [str(predicate["item_id"])]))
            elif kind == "location" and predicate.get("location_id") is not None:
                checks.append(("locations", [str(predicate["location_id"])]))
            elif kind == "combat_outcome" and predicate.get("encounter_id") is not None:
                checks.append(("encounters", [str(predicate["encounter_id"])]))

    if fragment_kind == "cast":
        checks.append(
            ("actors", [str(item.get("id")) for item in fragment.get("cast", [])])
        )
    elif fragment_kind == "locations":
        locations = fragment.get("locations", [])
        checks.append(("locations", [str(item.get("id")) for item in locations]))
        checks.append(
            (
                "locations",
                [
                    str(value)
                    for item in locations
                    for value in item.get("intra_exits", [])
                ],
            )
        )
    elif fragment_kind.startswith("act:") or fragment_kind == "endings":
        beats = fragment.get("beats", [])
        add_trigger_references(
            [
                trigger
                for beat in beats
                for trigger in beat.get("advance_conditions", [])
            ]
        )
        checks.extend(
            [
                ("beats", [str(item.get("id")) for item in beats]),
                (
                    "clues",
                    [
                        str(clue.get("id"))
                        for beat in beats
                        for clue in beat.get("key_info", [])
                    ],
                ),
                (
                    "encounters",
                    [
                        str(beat["encounter"].get("id"))
                        for beat in beats
                        if isinstance(beat.get("encounter"), dict)
                    ],
                ),
                (
                    "triggers",
                    [
                        str(trigger.get("id"))
                        for beat in beats
                        for trigger in beat.get("advance_conditions", [])
                    ],
                ),
                (
                    "locations",
                    [
                        str(location_id)
                        for beat in beats
                        for location_id in beat.get("location_ids", [])
                    ]
                    + [
                        str(value)
                        for beat in beats
                        for value in [
                            (beat.get("entry_state") or {}).get("location_id")
                        ]
                        if value is not None
                    ],
                ),
                (
                    "actors",
                    [
                        str(actor.get("actor_id") or actor.get("npc_ref"))
                        for beat in beats
                        for actor in (beat.get("entry_state") or {}).get("actors", [])
                    ]
                    + [
                        str(actor_id)
                        for beat in beats
                        if isinstance(beat.get("encounter"), dict)
                        for actor_id in beat["encounter"].get("monster_ids", [])
                    ],
                ),
                (
                    "beats",
                    [
                        str(exit_.get("next_beat_id"))
                        for beat in beats
                        for exit_ in beat.get("exits", [])
                    ],
                ),
                (
                    "clues",
                    [
                        str(clue_id)
                        for beat in beats
                        for clue_id in beat.get("relevant_clue_ids", [])
                    ]
                    + [
                        str(clue_id)
                        for beat in beats
                        if isinstance(beat.get("encounter"), dict)
                        for clue_id in beat["encounter"].get("on_win_discoveries", [])
                    ],
                ),
                (
                    "flags",
                    [
                        str(flag_id)
                        for beat in beats
                        for flag_id in beat.get("payoff_flag_ids", [])
                    ]
                    + [
                        str(flag_id)
                        for beat in beats
                        for clue in beat.get("key_info", [])
                        for flag_id in (clue.get("discovery_effects") or {}).get(
                            "flags_set", {}
                        )
                    ]
                    + [
                        str(flag_id)
                        for beat in beats
                        if isinstance(beat.get("encounter"), dict)
                        for flag_id in beat["encounter"].get("on_win_flags", [])
                    ],
                ),
                (
                    "items",
                    [
                        str(grant.get("item_id"))
                        for beat in beats
                        for clue in beat.get("key_info", [])
                        for grant in (clue.get("discovery_effects") or {}).get(
                            "grant_items", []
                        )
                    ],
                ),
            ]
        )
    elif fragment_kind == "actions":
        actions = fragment.get("action_definitions", [])
        checks.extend(
            [
                ("actions", [str(item.get("id")) for item in actions]),
                (
                    "flags",
                    [
                        str(value)
                        for action in actions
                        for value in (action.get("requirements") or {}).get("flags", [])
                    ]
                    + [
                        str(effect.get("flag"))
                        for action in actions
                        for effect in (action.get("contract") or {}).get(
                            "effect_templates", []
                        )
                        if effect.get("kind") == "set_flag"
                    ],
                ),
                (
                    "beats",
                    [
                        str(value)
                        for action in actions
                        for value in (action.get("requirements") or {}).get(
                            "beat_ids", []
                        )
                    ]
                    + [
                        str(effect.get("beat_id"))
                        for action in actions
                        for effect in (action.get("contract") or {}).get(
                            "effect_templates", []
                        )
                        if effect.get("kind") == "transition_beat"
                    ],
                ),
                (
                    "locations",
                    [
                        str(value)
                        for action in actions
                        for value in (action.get("requirements") or {}).get(
                            "location_ids", []
                        )
                    ]
                    + [
                        str(effect.get("location_id"))
                        for action in actions
                        for effect in (action.get("contract") or {}).get(
                            "effect_templates", []
                        )
                        if effect.get("kind") == "move_location"
                    ],
                ),
                (
                    "encounters",
                    [
                        str(value)
                        for action in actions
                        for value in (action.get("requirements") or {}).get(
                            "encounter_ids", []
                        )
                    ],
                ),
                (
                    "actors",
                    [
                        str(value)
                        for action in actions
                        for value in (action.get("targeting") or {}).get(
                            "actor_ids", []
                        )
                    ],
                ),
                (
                    "items",
                    [
                        str(effect.get("item_id"))
                        for action in actions
                        for effect in (action.get("contract") or {}).get(
                            "effect_templates", []
                        )
                        if effect.get("kind") in {"grant_item", "remove_item"}
                    ],
                ),
                (
                    "clues",
                    [
                        str(effect.get("clue_id"))
                        for action in actions
                        for effect in (action.get("contract") or {}).get(
                            "effect_templates", []
                        )
                        if effect.get("kind") == "discover_clue"
                    ],
                ),
            ]
        )
    elif fragment_kind == "top_level":
        checks.append(
            ("flags", [str(item) for item in fragment.get("declared_flags", [])])
        )
        checks.append(
            (
                "triggers",
                [
                    str(fragment[name].get("id"))
                    for name in ("win_condition", "lose_condition")
                    if isinstance(fragment.get(name), dict)
                ],
            )
        )
        add_trigger_references(
            [
                fragment[name]
                for name in ("win_condition", "lose_condition")
                if isinstance(fragment.get(name), dict)
            ]
        )
    for category, ids in checks:
        allowed = set(registry.get(category, []))
        for value in ids:
            if value not in allowed:
                errors.append(
                    f"分片 {fragment_kind} 创建或引用了计划外 {category} id «{value}»"
                )
    return errors


def validate_fragment_runtime(
    fragment_kind: str,
    fragment: dict[str, Any],
    plan: StoryPlan,
    compiled_fragments: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """在分片落库前解析枚举、卡面与规则行动，避免把损坏中间产物标为已验证。"""
    errors: list[str] = []
    compiled = compiled_fragments or {}
    registry = story_plan_id_registry(plan)

    if fragment_kind == "top_level":
        for name in ("win_condition", "lose_condition"):
            try:
                Trigger.from_dict(fragment[name])
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"top_level.{name} 无法解析：{exc}")
        return errors

    if fragment_kind == "cast":
        for raw in fragment.get("cast", []):
            try:
                npc = NpcSpec.from_dict(raw)
                if npc.card is None:
                    errors.append(f"新角色 «{npc.id}» 缺少固定 CombatCard")
                    continue
                if npc.card.get("current_hp") != npc.card.get("max_hp"):
                    errors.append(f"角色 «{npc.id}» 初始 current_hp 必须等于 max_hp")
                load_combatant({"type": "monster", "card": npc.card})
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"Cast 条目无法加载：{exc}")
        return errors

    if fragment_kind == "locations":
        allowed = set(registry["locations"])
        for raw in fragment.get("locations", []):
            try:
                location = LocationSpec.from_dict(raw)
                invalid = set(location.intra_exits) - allowed
                if invalid:
                    errors.append(
                        f"地点 «{location.id}» 的 intra_exits 含计划外地点 {sorted(invalid)}"
                    )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"Location 条目无法加载：{exc}")
        return errors

    if fragment_kind == "actions":
        for raw in fragment.get("action_definitions", []):
            try:
                action = ActionDefinition.from_dict(raw)
                action.validate()
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"ActionDefinition 无法执行：{exc}")
        return errors

    if not (fragment_kind.startswith("act:") or fragment_kind == "endings"):
        return [f"未知 fragment_kind：{fragment_kind}"]

    cast_by_id: dict[str, NpcSpec] = {}
    for raw in compiled.get("cast", {}).get("cast", []):
        try:
            npc = NpcSpec.from_dict(raw)
            cast_by_id[npc.id] = npc
        except (AttributeError, KeyError, TypeError, ValueError):
            # Cast 分片自身已在更早阶段校验，这里不重复报告同一个解析错误。
            continue
    allowed_locations = set(registry["locations"])
    for raw in fragment.get("beats", []):
        try:
            beat = Beat.from_dict(raw)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"Beat 条目无法加载：{exc}")
            continue
        invalid_locations = set(beat.location_ids) - allowed_locations
        if invalid_locations:
            errors.append(f"Beat «{beat.id}» 含计划外地点 {sorted(invalid_locations)}")
        for clue in beat.key_info:
            if not clue.discovery_hints:
                errors.append(f"线索 «{clue.id}» 缺少 discovery_hints")
        actor_entries = {
            str(item.get("actor_id") or item.get("npc_ref")): item
            for item in beat.entry_state.get("actors", [])
        }
        for actor_id, actor in actor_entries.items():
            card = actor.get("card") or (
                cast_by_id[actor_id].card if actor_id in cast_by_id else None
            )
            if card is None:
                errors.append(f"Beat «{beat.id}» 的 actor «{actor_id}» 缺少固定卡面")
                continue
            try:
                load_combatant({"type": actor.get("type", "monster"), "card": card})
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"Beat «{beat.id}» 的 actor «{actor_id}» 卡面非法：{exc}")
        if beat.encounter is not None:
            for monster_id in beat.encounter.monster_ids:
                if monster_id not in actor_entries:
                    errors.append(
                        f"Encounter «{beat.encounter.id}» 的 monster «{monster_id}» 不在本拍 entry_state"
                    )
        for trigger in beat.advance_conditions:
            if (
                trigger.kind == TriggerKind.COMBAT_OUTCOME
                and not trigger.predicate.get("encounter_id")
            ):
                errors.append(
                    f"combat_outcome Trigger «{trigger.id}» 缺少 encounter_id"
                )
    return errors


def validate_generated_canon(
    canon: Canon, brief: StoryDesignBrief | None = None
) -> list[str]:
    """只对新生成 Canon 启用的规模、分支、引用与运行时兼容校验。"""
    errors: list[str] = []
    if not canon.runtime_location_scoping:
        errors.append("新 Canon 必须启用 runtime_location_scoping")
    length_mode = canon.length_mode or infer_length_mode(canon.duration_minutes)
    limits = length_limits(length_mode)
    playable = [beat for beat in canon.beats if not beat.is_ending]
    endings = [beat for beat in canon.beats if beat.is_ending]
    acts = {beat.act_id for beat in playable if beat.act_id}
    if not limits["playable_beats"][0] <= len(playable) <= limits["playable_beats"][1]:
        errors.append(
            f"{length_mode} 可玩 Beat 必须在 {limits['playable_beats'][0]}–"
            f"{limits['playable_beats'][1]} 之间"
        )
    if len([beat for beat in endings if beat.ending_outcome == EndingOutcome.WIN]) != 1:
        errors.append("新 Canon 必须恰好有一个 win ending")
    if (
        len([beat for beat in endings if beat.ending_outcome == EndingOutcome.LOSE])
        != 1
    ):
        errors.append("新 Canon 必须恰好有一个 lose ending")
    if canon.act_count != len(acts):
        errors.append("Canon.act_count 必须等于可玩 Beat 的不同 act_id 数量")
    if not limits["acts"][0] <= canon.act_count <= limits["acts"][1]:
        errors.append(f"{length_mode} act_count 不符合规模档位")
    if (
        not limits["encounters"][0]
        <= sum(beat.encounter is not None for beat in playable)
        <= limits["encounters"][1]
    ):
        errors.append(f"{length_mode} Encounter 数量不符合规模档位")

    location_ids = {location.id for location in canon.locations}
    clue_ids = {clue.id for beat in canon.beats for clue in beat.key_info}
    declared_flags = set(canon.declared_flags)
    for beat in canon.beats:
        if not beat.act_id:
            errors.append(f"新 Canon Beat «{beat.id}» 缺少 act_id")
        if beat.estimated_minutes <= 0:
            errors.append(f"新 Canon Beat «{beat.id}» 缺少 estimated_minutes")
        if not beat.is_ending and (not beat.objective or not beat.pressure):
            errors.append(f"新 Canon Beat «{beat.id}» 必须填写 objective 与 pressure")
        semantic_count = sum(
            trigger.kind == TriggerKind.SEMANTIC for trigger in beat.advance_conditions
        )
        if semantic_count > 1:
            errors.append(f"Beat «{beat.id}» 最多只能有一个 semantic Trigger")
        targets = {exit_.next_beat_id for exit_ in beat.exits}
        if len(targets) > 1 and any(
            trigger.kind != TriggerKind.ACTION
            for trigger in beat.advance_conditions
            if beat.exit_for(trigger.id) is not None
        ):
            errors.append(
                f"分支 Beat «{beat.id}» 的不同出口必须全部使用 action Trigger"
            )
        for clue in beat.key_info:
            if not clue.location_id or clue.location_id not in beat.location_ids:
                errors.append(f"线索 «{clue.id}» 必须绑定本拍明确 location_id")
            if not clue.discovery_hints:
                errors.append(f"线索 «{clue.id}» 必须提供 discovery_hints")
        for clue_id in beat.relevant_clue_ids:
            if clue_id not in clue_ids:
                errors.append(
                    f"Beat «{beat.id}» relevant_clue_ids 引用不存在的线索 «{clue_id}»"
                )
        for flag_id in beat.payoff_flag_ids:
            if flag_id not in declared_flags:
                errors.append(
                    f"Beat «{beat.id}» payoff_flag_ids 引用未声明 flag «{flag_id}»"
                )
        if beat.encounter is not None:
            encounter = beat.encounter
            if (
                not encounter.location_id
                or encounter.location_id not in beat.location_ids
            ):
                errors.append(
                    f"Encounter «{encounter.id}» 必须绑定本拍明确 location_id"
                )
            for monster_id in encounter.monster_ids:
                spec = canon.npc(monster_id)
                actor = next(
                    (
                        item
                        for item in beat.entry_state.get("actors", [])
                        if (item.get("actor_id") or item.get("npc_ref")) == monster_id
                    ),
                    None,
                )
                card = (actor or {}).get("card") or (spec.card if spec else None)
                try:
                    load_combatant(
                        {"type": (actor or {}).get("type", "monster"), "card": card}
                    )
                except Exception as exc:
                    errors.append(f"敌人卡面 «{monster_id}» 无法加载：{exc}")
                if actor and actor.get("location_id") != encounter.location_id:
                    errors.append(
                        f"Encounter «{encounter.id}» 的敌人 «{monster_id}» 不在遭遇地点"
                    )
    encounter_ids = {
        beat.encounter.id for beat in canon.beats if beat.encounter is not None
    }
    conditions = [
        *(beat.advance_conditions for beat in canon.beats),
    ]
    for trigger in [
        *(item for group in conditions for item in group),
        *(
            item
            for item in (canon.win_condition, canon.lose_condition)
            if item is not None
        ),
    ]:
        if trigger.kind == TriggerKind.COMBAT_OUTCOME:
            encounter_id = trigger.predicate.get("encounter_id")
            if not encounter_id:
                errors.append(
                    f"combat_outcome Trigger «{trigger.id}» 必须绑定 encounter_id"
                )
            elif encounter_id not in encounter_ids:
                errors.append(
                    f"combat_outcome Trigger «{trigger.id}» 引用不存在的 encounter"
                )

    for npc in canon.cast:
        if npc.card is None:
            continue
        for attack in npc.card.get("attacks", []):
            try:
                parse_dice(str(attack.get("damage_dice") or ""))
            except ValueError as exc:
                errors.append(f"卡面 «{npc.id}» 骰式无效：{exc}")
    for action in canon.action_definitions:
        try:
            action.validate()
        except (TypeError, ValueError) as exc:
            errors.append(f"规则行动 «{action.id}» 无法执行：{exc}")

    if brief is not None:
        if canon.length_mode != brief.length_mode:
            errors.append("Canon.length_mode 必须与确认设计稿一致")
        if canon.duration_minutes != brief.duration_minutes:
            errors.append("Canon.duration_minutes 必须与确认设计稿一致")
        if canon.recommended_player_count != brief.player_count:
            errors.append("Canon.recommended_player_count 必须与确认设计稿一致")
        if brief.scale_profile:
            expected = brief.scale_profile
            actual = {
                "playable_beats": len(playable),
                "acts": canon.act_count,
                "locations": len(canon.locations),
                "encounters": sum(beat.encounter is not None for beat in playable),
                "clues": len(clue_ids),
            }
            for name, value in expected.model_dump().items():
                if actual[name] != value:
                    errors.append(f"Canon {name} 必须等于确认目标 {value}")
    return errors


def validate_effect_owner_ledger(canon: Canon, plan: StoryPlan) -> list[str]:
    """确保最终 Canon 的每个原子写入仍由计划锁定的唯一 owner 承担。"""
    errors: list[str] = []
    flag_sources = managed_flag_sources(canon)
    item_sources: dict[str, list[tuple[str, str]]] = {}
    for beat in canon.beats:
        for clue in beat.key_info:
            for grant in (clue.discovery_effects or {}).get("grant_items", []):
                item_id = str(grant.get("item_id") or "")
                if item_id:
                    item_sources.setdefault(item_id, []).append(("discovery", clue.id))
    for action in canon.action_definitions:
        for effect in action.contract.get("effect_templates", []):
            if effect.get("kind") == "grant_item" and effect.get("item_id"):
                item_sources.setdefault(str(effect["item_id"]), []).append(
                    ("rule_action", action.id)
                )

    for owner in plan.effect_owner_ledger:
        if owner.effect_kind == "flag":
            actual = [
                (str(source.get("kind")), str(source.get("owner_id")))
                for source in flag_sources.get(owner.effect_id, [])
            ]
        else:
            actual = item_sources.get(owner.effect_id, [])
        expected = (
            []
            if owner.owner_kind == "dm_free_write"
            else [(owner.owner_kind, owner.owner_id)]
        )
        if actual != expected:
            errors.append(
                f"效果 «{owner.effect_id}» 的最终 owner {actual} 与计划 {expected} 不一致"
            )
    return errors


def canon_quality_metrics(
    canon: Canon, *, repair_count: int = 0, continuity_passed: bool = False
) -> StoryQualityMetrics:
    """派生公开指标，不包含计划、Canon 正文、NPC 秘密或谜底。"""
    adjacency = {
        beat.id: [exit_.next_beat_id for exit_ in beat.exits] for beat in canon.beats
    }
    endings = {beat.id for beat in canon.beats if beat.is_ending}
    minutes = {beat.id: max(0, beat.estimated_minutes) for beat in canon.beats}
    paths = _path_minutes(canon.start_beat_id, adjacency, minutes, endings)
    playable = [beat for beat in canon.beats if not beat.is_ending]
    return StoryQualityMetrics(
        act_count=canon.act_count,
        playable_beat_count=len(playable),
        location_count=len(canon.locations),
        clue_count=sum(len(beat.key_info) for beat in canon.beats),
        encounter_count=sum(beat.encounter is not None for beat in playable),
        branch_count=sum(
            len({exit_.next_beat_id for exit_ in beat.exits}) > 1 for beat in playable
        ),
        semantic_trigger_count=sum(
            trigger.kind == TriggerKind.SEMANTIC
            for beat in canon.beats
            for trigger in beat.advance_conditions
        ),
        shortest_minutes=min(paths) if paths else canon.duration_minutes,
        longest_minutes=max(paths) if paths else canon.duration_minutes,
        repair_count=repair_count,
        continuity_passed=continuity_passed,
        quality_notes=[] if continuity_passed else ["连贯性复核未通过"],
    )


def _find_cycle(adjacency: dict[str, list[str]]) -> list[str]:
    colors: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        colors[node] = 1
        stack.append(node)
        for target in adjacency.get(node, []):
            if colors.get(target, 0) == 0:
                cycle = visit(target)
                if cycle:
                    return cycle
            elif colors.get(target) == 1:
                index = stack.index(target)
                return [*stack[index:], target]
        stack.pop()
        colors[node] = 2
        return []

    for node in adjacency:
        if colors.get(node, 0) == 0 and (cycle := visit(node)):
            return cycle
    return []


def _reachable(start: str, adjacency: dict[str, list[str]]) -> set[str]:
    reached: set[str] = set()
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node in reached:
            continue
        reached.add(node)
        queue.extend(adjacency.get(node, []))
    return reached


def _reverse_graph(adjacency: dict[str, list[str]]) -> dict[str, list[str]]:
    result = {node: [] for node in adjacency}
    for node, targets in adjacency.items():
        for target in targets:
            result.setdefault(target, []).append(node)
    return result


def _shortest_distance(
    start: str, target: str, adjacency: dict[str, list[str]]
) -> int | None:
    queue = deque([(start, 0)])
    seen: set[str] = set()
    while queue:
        node, distance = queue.popleft()
        if node == target:
            return distance
        if node in seen:
            continue
        seen.add(node)
        queue.extend((next_node, distance + 1) for next_node in adjacency.get(node, []))
    return None


def _topological_order(adjacency: dict[str, list[str]]) -> list[str]:
    indegree = {node: 0 for node in adjacency}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] = indegree.get(target, 0) + 1
    queue = deque(node for node, degree in indegree.items() if degree == 0)
    result: list[str] = []
    while queue:
        node = queue.popleft()
        result.append(node)
        for target in adjacency.get(node, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return result


def _path_minutes(
    start: str,
    adjacency: dict[str, list[str]],
    minutes: dict[str, int],
    ending_ids: set[str],
) -> list[int]:
    if _find_cycle(adjacency):
        return []
    memo: dict[str, list[int]] = {}

    def visit(node: str) -> list[int]:
        if node in memo:
            return memo[node]
        own = minutes.get(node, 0)
        if node in ending_ids:
            memo[node] = [own]
            return memo[node]
        tails = [value for target in adjacency.get(node, []) for value in visit(target)]
        memo[node] = [own + value for value in tails]
        return memo[node]

    return visit(start)


def _beat_paths(
    start: str, adjacency: dict[str, list[str]], ending_ids: set[str]
) -> list[list[str]]:
    """枚举小型 StoryPlan DAG 的起点到结局路径，供节奏分桶校验。"""
    if _find_cycle(adjacency):
        return []
    if start in ending_ids:
        return [[start]]
    paths: list[list[str]] = []
    for target in adjacency.get(start, []):
        paths.extend(
            [[start, *tail] for tail in _beat_paths(target, adjacency, ending_ids)]
        )
    return paths
