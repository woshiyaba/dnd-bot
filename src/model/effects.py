"""挂在参战者身上的「轻量引用 / 计数」数据模型。

包含三类，均对应 docs/原始数据.md：
- Condition（1.8 当前状态）：每回合结算的增益/减益。
- LearnedSkill（1.6）：引用封闭的技能定义，记录其消耗状态。
- InventoryItem（1.7）：引用封闭的道具定义，记录数量。

技能/道具的「机械效果」落在各自封闭定义（效果积木）里，本版只存引用与计数。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.enums import ConditionType, DamageType


@dataclass(slots=True)
class Condition:
    """参战者身上的一条状态，每回合开始时由引擎结算。

    持续伤害 类状态用 `amount` + `damage_type` 描述每回合扣血；其余状态二者留空。
    """

    kind: ConditionType  # 状态类型
    rounds_left: int = 1  # 剩余回合
    amount: int = 0  # 数值：仅持续伤害使用，每回合扣的固定 HP
    damage_type: DamageType | None = None  # 伤害类型：仅持续伤害使用，灼烧/流血等

    @property
    def is_expired(self) -> bool:
        """是否已过期（剩余回合归零）。"""
        return self.rounds_left <= 0

    @classmethod
    def from_dict(cls, data: dict) -> "Condition":
        """从字典构造一条状态。"""
        raw_damage_type = data.get("damage_type")
        return cls(
            kind=ConditionType(data["kind"]),
            rounds_left=int(data.get("rounds_left", 1)),
            amount=int(data.get("amount", 0)),
            damage_type=DamageType(raw_damage_type) if raw_damage_type else None,
        )

    def to_dict(self) -> dict:
        """导出为字典（仅持续伤害带 amount/damage_type）。"""
        result = {"kind": self.kind.value, "rounds_left": self.rounds_left}
        if self.kind == ConditionType.DAMAGE_OVER_TIME:
            result["amount"] = self.amount
            if self.damage_type:
                result["damage_type"] = self.damage_type.value
        return result


@dataclass(slots=True)
class LearnedSkill:
    """指向封闭技能定义的引用 + 消耗状态。"""

    skill_id: str  # 技能 id
    charges: int = 0  # 当前充能
    cooldown_left: int = 0  # 冷却剩余

    @property
    def is_available(self) -> bool:
        """是否可用：有充能且不在冷却中。"""
        return self.charges > 0 and self.cooldown_left <= 0

    @classmethod
    def from_dict(cls, data: dict) -> "LearnedSkill":
        """从字典构造一条已学技能。"""
        return cls(
            skill_id=data["skill_id"],
            charges=int(data.get("charges", 0)),
            cooldown_left=int(data.get("cooldown_left", 0)),
        )

    def to_dict(self) -> dict:
        """导出为字典。"""
        return {
            "skill_id": self.skill_id,
            "charges": self.charges,
            "cooldown_left": self.cooldown_left,
        }


@dataclass(slots=True)
class InventoryItem:
    """指向封闭道具定义的引用 + 数量。"""

    item_id: str  # 道具 id
    quantity: int = 1  # 数量

    @property
    def is_available(self) -> bool:
        """是否可用：尚有数量。"""
        return self.quantity > 0

    @classmethod
    def from_dict(cls, data: dict) -> "InventoryItem":
        """从字典构造一条背包道具。"""
        return cls(item_id=data["item_id"], quantity=int(data.get("quantity", 1)))

    def to_dict(self) -> dict:
        """导出为字典。"""
        return {"item_id": self.item_id, "quantity": self.quantity}
