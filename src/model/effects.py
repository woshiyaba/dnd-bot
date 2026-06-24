"""挂在参战者身上的「轻量引用 / 计数」数据模型。

包含三类，均对应 docs/原始数据.md：
- 状态效果（1.8 当前状态）：每回合结算的增益/减益。
- 已学技能（1.6）：引用封闭的技能定义，记录其消耗状态。
- 背包道具（1.7）：引用封闭的道具定义，记录数量。

技能/道具的「机械效果」落在各自封闭定义（效果积木）里，本版只存引用与计数。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.enums import 伤害类型, 状态类型


@dataclass(slots=True)
class 状态效果:
    """参战者身上的一条状态，每回合开始时由引擎结算。

    持续伤害 类状态用 `数值` + `伤害类型` 描述每回合扣血；其余状态二者留空。
    """

    状态: 状态类型
    剩余回合: int = 1
    数值: int = 0                       # 仅 持续伤害 使用：每回合扣的固定 HP
    伤害类型: 伤害类型 | None = None     # 仅 持续伤害 使用：灼烧/流血等

    @property
    def 已过期(self) -> bool:
        return self.剩余回合 <= 0

    @classmethod
    def from_dict(cls, data: dict) -> "状态效果":
        原始伤害类型 = data.get("伤害类型")
        return cls(
            状态=状态类型(data["状态"]),
            剩余回合=int(data.get("剩余回合", 1)),
            数值=int(data.get("数值", 0)),
            伤害类型=伤害类型(原始伤害类型) if 原始伤害类型 else None,
        )

    def to_dict(self) -> dict:
        结果 = {"状态": self.状态.value, "剩余回合": self.剩余回合}
        if self.状态 == 状态类型.持续伤害:
            结果["数值"] = self.数值
            if self.伤害类型:
                结果["伤害类型"] = self.伤害类型.value
        return 结果


@dataclass(slots=True)
class 已学技能:
    """指向封闭技能定义的引用 + 消耗状态。"""

    技能id: str
    当前充能: int = 0
    冷却剩余: int = 0

    @property
    def 可用(self) -> bool:
        return self.当前充能 > 0 and self.冷却剩余 <= 0

    @classmethod
    def from_dict(cls, data: dict) -> "已学技能":
        return cls(
            技能id=data["技能id"],
            当前充能=int(data.get("当前充能", 0)),
            冷却剩余=int(data.get("冷却剩余", 0)),
        )

    def to_dict(self) -> dict:
        return {
            "技能id": self.技能id,
            "当前充能": self.当前充能,
            "冷却剩余": self.冷却剩余,
        }


@dataclass(slots=True)
class 背包道具:
    """指向封闭道具定义的引用 + 数量。"""

    道具id: str
    数量: int = 1

    @property
    def 可用(self) -> bool:
        return self.数量 > 0

    @classmethod
    def from_dict(cls, data: dict) -> "背包道具":
        return cls(道具id=data["道具id"], 数量=int(data.get("数量", 1)))

    def to_dict(self) -> dict:
        return {"道具id": self.道具id, "数量": self.数量}
