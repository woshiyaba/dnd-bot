"""攻击手段数据模型。

对应 docs/原始数据.md 1.5「攻击手段」：直接存算好的最终数值，最省事。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.enums import DamageType, Range


@dataclass(slots=True)
class Attack:
    """角色/怪物的一种攻击方式（长剑、火焰弹、弯刀…）。

    命中加值 / 伤害骰 都是预先算好的最终值，引擎直接读取，不再叠加属性调整值。
    """

    name: str               # 名字
    attack_bonus: int       # 命中加值
    damage_dice: str        # 伤害骰：骰子表达式，如 "1d8+3"
    damage_type: DamageType = DamageType.SLASHING  # 伤害类型
    attack_range: Range = Range.MELEE              # 射程

    @property
    def is_ranged(self) -> bool:
        """是否远程攻击。"""
        return self.attack_range == Range.RANGED

    @classmethod
    def from_dict(cls, data: dict) -> "Attack":
        """从卡面字典（英文键）构造。容忍字符串形式的枚举值。"""
        return cls(
            name=data["name"],
            attack_bonus=int(data["attack_bonus"]),
            damage_dice=str(data["damage_dice"]),
            damage_type=DamageType(data.get("damage_type", DamageType.SLASHING)),
            attack_range=Range(data.get("range", Range.MELEE)),
        )

    def to_dict(self) -> dict:
        """导出卡面字典（英文键）。"""
        return {
            "name": self.name,
            "attack_bonus": self.attack_bonus,
            "damage_dice": self.damage_dice,
            "damage_type": self.damage_type.value,
            "range": self.attack_range.value,
        }
