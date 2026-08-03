"""StoryPlan 校验问题分类、收敛指纹与区段合并。"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

STORY_PLAN_DEPENDENCY_CLOSURE = {
    "acts",
    "beats",
    "entities",
    "clue_graph",
    "branch_points",
    "foreshadowing_payoffs",
    "ending_routes",
    "effect_owner_ledger",
}


class PlanValidationIssue(BaseModel):
    """可用于修复分流和错误去重的 StoryPlan 问题。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    path: tuple[str | int, ...]
    category: Literal["local", "structural"]
    affected_sections: frozenset[str]
    message: str


def story_plan_field_issues(exc: ValidationError) -> list[PlanValidationIssue]:
    """把候选或最终 schema 错误转成稳定的结构化问题。"""
    issues: list[PlanValidationIssue] = []
    for item in exc.errors(include_url=False):
        path = tuple(item.get("loc", ()))
        error_type = str(item.get("type", "validation_error"))
        root = str(path[0]) if path else "<root>"
        location = ".".join(str(part) for part in path) or "<root>"
        message = (
            f"StoryPlan 字段不合法：{location}: "
            f"{item.get('msg', '校验失败')} [{error_type}]"
        )
        issues.append(
            PlanValidationIssue(
                code=f"schema_{error_type}",
                path=path,
                category=_field_issue_category(path, error_type),
                affected_sections=frozenset(
                    {root} if root != "<root>" else STORY_PLAN_DEPENDENCY_CLOSURE
                ),
                message=message,
            )
        )
    return issues


def classify_story_plan_issues(errors: list[str]) -> list[PlanValidationIssue]:
    """为现有确定性校验消息补充代码、路径、类别和影响区段。"""
    return [
        PlanValidationIssue(
            code=_issue_code(message),
            path=_issue_path(message),
            category="structural" if _is_structural(message) else "local",
            affected_sections=frozenset(_affected_sections(message)),
            message=message,
        )
        for message in errors
    ]


def story_plan_issue_fingerprint(
    issues: list[PlanValidationIssue],
) -> tuple[tuple[str, tuple[str | int, ...]], ...]:
    """忽略自然语言细节，对问题代码和路径生成稳定指纹。"""
    entries = [(issue.code, issue.path) for issue in issues]
    return tuple(sorted(entries, key=lambda item: (item[0], tuple(map(str, item[1])))))


def affected_story_plan_sections(
    issues: list[PlanValidationIssue],
) -> set[str]:
    """汇总局部修复有权替换的顶层区段。"""
    return {section for issue in issues for section in issue.affected_sections}


def merge_story_plan_sections(
    *,
    previous: dict[str, Any],
    repair: dict[str, Any],
    allowed_sections: set[str],
) -> dict[str, Any]:
    """只合并获授权的完整顶层区段。"""
    if repair.get("repair_kind") != "story_plan_sections":
        raise ValueError("StoryPlan 局部修复缺少 repair_kind=story_plan_sections")
    sections = repair.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("StoryPlan 局部修复必须返回 sections 对象")
    missing = sorted(allowed_sections - set(sections))
    if missing:
        raise ValueError("StoryPlan 局部修复缺少完整区段：" + "、".join(missing))

    merged = deepcopy(previous)
    for section in allowed_sections:
        merged[section] = deepcopy(sections[section])
    if _story_plan_structure_signature(merged) != _story_plan_structure_signature(
        previous
    ):
        raise ValueError("StoryPlan 局部修复不得修改对象 ID、所属关系或 Beat 拓扑")
    return merged


def _story_plan_structure_signature(raw: dict[str, Any]) -> tuple[Any, ...]:
    """提取局部修复必须保持不变的对象集合与图结构。"""

    def values(name: str) -> list[dict[str, Any]]:
        value = raw.get(name)
        return (
            [item for item in value if isinstance(item, dict)]
            if isinstance(value, list)
            else []
        )

    entities = raw.get("entities")
    entities = entities if isinstance(entities, dict) else {}
    entity_ids = tuple(
        (
            category,
            tuple(
                item.get("id")
                for item in entities.get(category, [])
                if isinstance(item, dict)
            ),
        )
        for category in (
            "actors",
            "locations",
            "encounters",
            "clues",
            "flags",
            "items",
            "actions",
        )
    )
    acts = tuple(item.get("id") for item in values("acts"))
    beats = tuple(
        (
            item.get("id"),
            item.get("act_id"),
            tuple(
                exit_.get("to_beat_id")
                for exit_ in item.get("exits", [])
                if isinstance(exit_, dict)
            ),
        )
        for item in values("beats")
    )
    branches = tuple(
        (item.get("beat_id"), item.get("reconverge_at"))
        for item in values("branch_points")
    )
    ending_ids = tuple(item.get("ending_id") for item in values("ending_routes"))
    return (
        raw.get("campaign_id_candidate"),
        raw.get("start_beat_id"),
        acts,
        beats,
        branches,
        ending_ids,
        entity_ids,
    )


def _field_issue_category(
    path: tuple[str | int, ...], error_type: str
) -> Literal["local", "structural"]:
    names = {str(part) for part in path}
    structural_id_fields = {
        "id",
        "campaign_id_candidate",
        "start_beat_id",
        "act_id",
        "to_beat_id",
        "beat_id",
        "ending_id",
        "reconverge_at",
    }
    if names & structural_id_fields:
        return "structural"
    if error_type == "extra_forbidden" and len(path) == 1:
        return "structural"
    if path and path[0] in {"acts", "beats", "entities", "ending_routes"}:
        if len(path) == 1 or (error_type == "list_type" and len(path) <= 2):
            return "structural"
    if "beat_ids" in names and error_type == "too_short":
        return "structural"
    if (
        path
        and path[0] == "branch_points"
        and "choices" in names
        and error_type in {"too_short", "too_long"}
    ):
        return "structural"
    return "local"


def _issue_code(message: str) -> str:
    rules = (
        ("数量", "target_count_mismatch"),
        ("跨类别重复", "cross_category_id_conflict"),
        (" id «", "duplicate_id"),
        ("必须是 DAG", "cycle_detected"),
        ("从起点不可达", "unreachable_beat"),
        ("不存在通往结局", "ending_unreachable"),
        ("没有从起点到结局", "missing_complete_path"),
        ("branch_points 必须与", "branch_registry_mismatch"),
        ("分支数为", "branch_count_mismatch"),
        ("必须在 1–2 Beat 后汇流", "branch_reconvergence_invalid"),
        ("必须在高潮前汇流", "branch_reconvergence_late"),
        ("choices 必须等于", "branch_choices_mismatch"),
        ("owner", "effect_owner_invalid"),
        ("缺少唯一 owner", "effect_owner_missing"),
        ("伏笔", "payoff_invalid"),
        ("payoff flag", "payoff_invalid"),
        ("节奏", "pacing_mismatch"),
        ("路径", "path_invalid"),
    )
    for marker, code in rules:
        if marker in message:
            return code
    return re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:64] or "invalid"


def _issue_path(message: str) -> tuple[str | int, ...]:
    count_match = re.search(r"StoryPlan ([a-z_]+) 数量", message)
    if count_match:
        return (count_match.group(1),)
    pacing_match = re.search(r"路径 (\d+) 的 ([a-z_]+) 节奏", message)
    if pacing_match:
        return ("beats", int(pacing_match.group(1)), pacing_match.group(2))
    if message.startswith("最短路径"):
        return ("beats", "shortest_path")
    if message.startswith("最长路径"):
        return ("beats", "longest_path")
    quoted = re.findall(r"«([^»]+)»", message)
    sections = _affected_sections(message)
    root = sorted(sections)[0] if sections else "story_plan"
    return (root, *quoted[:2])


def _is_structural(message: str) -> bool:
    structural_markers = (
        "数量",
        "重复",
        "跨类别",
        "start_beat_id 不存在",
        "引用了不存在的 Act",
        "引用了不存在的 Beat",
        "指向不存在的 Beat",
        "必须且只能属于其 act_id",
        "非结局 Beat",
        "结局 Beat",
        "必须是 DAG",
        "从起点不可达",
        "不存在通往结局",
        "没有从起点到结局",
        "ending_routes 必须与",
        "分支数为",
        "branch_points 必须与",
        "分支引用不存在",
        "汇流点",
        "必须在 1–2 Beat 后汇流",
        "必须在高潮前汇流",
    )
    return any(marker in message for marker in structural_markers)


def _affected_sections(message: str) -> set[str]:
    sections: set[str] = set()
    if "scale_profile" in message:
        sections.add("scale_profile")
    if any(marker in message for marker in ("Act «", "Beat «", "路径", "节奏")):
        sections.add("beats")
    if "Act «" in message:
        sections.add("acts")
    if any(marker in message for marker in ("分支", "branch_points", "汇流")):
        sections.update({"beats", "branch_points"})
    if any(marker in message for marker in ("伏笔", "payoff")):
        sections.update({"beats", "foreshadowing_payoffs"})
    if any(marker in message for marker in ("owner", "效果 «")):
        sections.add("effect_owner_ledger")
    if any(marker in message for marker in ("线索 «", "clue_graph")):
        sections.update({"entities", "clue_graph", "beats"})
    if any(marker in message for marker in ("结局", "ending_routes")):
        sections.update({"beats", "ending_routes"})
    if (
        "locations 数量" in message
        or "encounters 数量" in message
        or "clues 数量" in message
    ):
        sections.update({"entities", "beats", "clue_graph"})
    if "playable_beats 数量" in message or "acts 数量" in message:
        sections.update({"acts", "beats"})
    return sections or set(STORY_PLAN_DEPENDENCY_CLOSURE)
