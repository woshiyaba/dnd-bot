"""技能、物品与任务特性的统一规则行动定义。

本模块只描述可持久化的领域数据，不依赖 LangGraph、DM 或战斗执行器。
``ActionDefinition`` 是作者/技能目录给引擎的能力边界；LLM 只能在这个边界内
把玩家选择编译成 ``ActionPlan``，不能自由创造机械效果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACTION_SCHEMA_VERSION = 2
ACTION_SOURCE_KINDS = {"skill", "item", "quest_feature"}
ACTION_SCOPES = {"combat", "world"}
USAGE_KINDS = {
    "unlimited",
    "skill_resource",
    "consume_item",
    "once_per_combat",
    "once_per_session",
}
ACTION_CHECK_KINDS = {"attack_roll", "saving_throw", "ability_check"}
COMBAT_EFFECT_KINDS = {
    "damage",
    "healing",
    "temporary_hp",
    "add_condition",
    "remove_condition",
    "modify_ac",
    "modify_attack_bonus",
    "move_zone",
    "revive",
}
WORLD_EFFECT_KINDS = {
    "damage",
    "healing",
    "temporary_hp",
    "add_condition",
    "remove_condition",
    "revive",
    "set_flag",
    "grant_item",
    "remove_item",
    "discover_clue",
    "move_location",
    "transition_beat",
}
TARGET_MODES = {"selected_each", "selected_one", "actor", "none"}


@dataclass(slots=True)
class ActionDefinition:
    """一项可执行规则行动的只读定义。

    ``contract`` 中的 ``check_templates`` / ``effect_templates`` 是机械白名单；
    LLM 计划只负责实例化模板、绑定合法目标与条件分支。
    """

    id: str
    name: str
    source_kind: str
    source_ref: str
    scopes: tuple[str, ...]
    description: str = ""
    requirements: dict[str, Any] = field(default_factory=dict)
    targeting: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=lambda: {"kind": "unlimited"})
    contract: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionDefinition":
        """从 Canon 或运行时目录字典构造并校验定义。"""
        definition = cls(
            id=str(data.get("id") or "").strip(),
            name=str(data.get("name") or data.get("label") or "").strip(),
            source_kind=str(data.get("source_kind") or "").strip(),
            source_ref=str(data.get("source_ref") or "").strip(),
            scopes=tuple(str(value) for value in data.get("scopes", [])),
            description=str(data.get("description") or "").strip(),
            requirements=dict(data.get("requirements") or {}),
            targeting=dict(data.get("targeting") or {}),
            usage=dict(data.get("usage") or {"kind": "unlimited"}),
            contract=dict(data.get("contract") or {}),
        )
        definition.validate()
        return definition

    def validate(self) -> None:
        """校验定义的结构边界；非法 Canon 在加载时立即失败。"""
        if not self.id or not self.name:
            raise ValueError("规则行动必须提供 id 与 name")
        if self.source_kind not in ACTION_SOURCE_KINDS:
            raise ValueError(f"规则行动 «{self.id}» 的 source_kind 无效")
        if not self.source_ref:
            raise ValueError(f"规则行动 «{self.id}» 缺少 source_ref")
        if not self.scopes or not set(self.scopes).issubset(ACTION_SCOPES):
            raise ValueError(f"规则行动 «{self.id}» 的 scopes 无效")
        usage_kind = str(self.usage.get("kind") or "")
        if usage_kind not in USAGE_KINDS:
            raise ValueError(f"规则行动 «{self.id}» 的 usage.kind 无效")
        if usage_kind == "once_per_combat" and "world" in self.scopes:
            raise ValueError(
                f"规则行动 «{self.id}» 的 once_per_combat 只能用于 combat scope"
            )
        if usage_kind == "consume_item" and int(self.usage.get("quantity", 1)) < 1:
            raise ValueError(f"规则行动 «{self.id}» 的物品消耗数量必须大于零")
        checks = self.contract.get("check_templates", [])
        effects = self.contract.get("effect_templates", [])
        if not isinstance(checks, list) or not isinstance(effects, list) or not effects:
            raise ValueError(f"规则行动 «{self.id}» 缺少效果模板")
        if any(not isinstance(item, dict) for item in [*checks, *effects]):
            raise ValueError(f"规则行动 «{self.id}» 的模板必须是对象")
        ids = [str(item.get("id") or "") for item in [*checks, *effects]]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"规则行动 «{self.id}» 的模板 id 为空或重复")
        for check in checks:
            if check.get("kind") not in ACTION_CHECK_KINDS:
                raise ValueError(
                    f"规则行动 «{self.id}» 的检定类型 «{check.get('kind')}» 无效"
                )
            if str(check.get("target_mode") or "none") not in TARGET_MODES:
                raise ValueError(f"规则行动 «{self.id}» 的检定目标模式无效")
        allowed_by_scope = {
            "combat": COMBAT_EFFECT_KINDS,
            "world": WORLD_EFFECT_KINDS,
        }
        for effect in effects:
            kind = str(effect.get("kind") or "")
            invalid_scopes = [
                scope for scope in self.scopes if kind not in allowed_by_scope[scope]
            ]
            if invalid_scopes:
                raise ValueError(
                    f"规则行动 «{self.id}» 的效果 «{kind}» 不支持 scope "
                    f"{', '.join(invalid_scopes)}"
                )
            if str(effect.get("target_mode") or "none") not in TARGET_MODES:
                raise ValueError(f"规则行动 «{self.id}» 的效果目标模式无效")

    def to_dict(self) -> dict[str, Any]:
        """导出给执行器、LLM 与前端共用的稳定字典。"""
        return {
            "schema_version": ACTION_SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "scopes": list(self.scopes),
            "description": self.description,
            "requirements": dict(self.requirements),
            "targeting": dict(self.targeting),
            "usage": dict(self.usage),
            "contract": dict(self.contract),
        }
