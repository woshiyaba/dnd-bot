"""攻击手段数据模型。

对应 docs/原始数据.md 1.5「攻击手段」：直接存算好的最终数值，最省事。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.enums import 伤害类型, 射程


@dataclass(slots=True)
class 攻击手段:
    """角色/怪物的一种攻击方式（长剑、火焰弹、弯刀…）。

    命中加值 / 伤害骰 都是预先算好的最终值，引擎直接读取，不再叠加属性调整值。
    """

    名字: str
    命中加值: int
    伤害骰: str          # 骰子表达式，如 "1d8+3"
    伤害类型: 伤害类型 = 伤害类型.挥砍
    射程: 射程 = 射程.近战

    @property
    def 是远程(self) -> bool:
        return self.射程 == 射程.远程

    @classmethod
    def from_dict(cls, data: dict) -> "攻击手段":
        """从卡面字典（中文键）构造。容忍字符串形式的枚举值。"""
        return cls(
            名字=data["名字"],
            命中加值=int(data["命中加值"]),
            伤害骰=str(data["伤害骰"]),
            伤害类型=伤害类型(data.get("伤害类型", 伤害类型.挥砍)),
            射程=射程(data.get("射程", 射程.近战)),
        )

    def to_dict(self) -> dict:
        return {
            "名字": self.名字,
            "命中加值": self.命中加值,
            "伤害骰": self.伤害骰,
            "伤害类型": self.伤害类型.value,
            "射程": self.射程.value,
        }
