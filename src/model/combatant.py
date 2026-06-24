"""参战者数据模型（继承体系）。

设计目标（按用户要求「尽量抽象」）：怪物 / 角色 / NPC 的**通用数据**只定义一次，
放在公共父类 `参战者`；各自**独有**的字段由子类追加，绝不重复定义。

继承关系::

    参战者 (Combatant)                # 引擎每回合都要读的最小子集 == 怪物卡
    ├── 怪物   (Monster)              # 就是参战者本身，默认敌人阵营、DM 操控
    └── 角色   (Character)            # 追加完整卡面：种族/职业/熟练/技能/背包
        ├── 玩家角色 (PlayerCharacter) # 玩家操控，先攻/攻击/豁免靠中断报骰
        └── 非玩家角色 (NPC)           # 同样的完整卡面，但由 DM 操控

字段命名沿用 docs/原始数据.md 的中文键，便于直接从卡面 JSON 加载、写回世界库。
派生值（属性调整值、熟练加值）一律现算，不入卡。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.model.attack import 攻击手段
from src.model.effects import 已学技能, 状态效果, 背包道具
from src.model.enums import 属性, 存活状态, 状态类型, 阵营


def 属性调整值(属性值: int) -> int:
    """`(属性值 − 10) ÷ 2`，向下取整。负数也正确向下取整。"""
    return (属性值 - 10) // 2


def 等级熟练加值(等级: int) -> int:
    """由等级派生熟练加值：1 级 +2，之后每 4 级 +1（5e 通用公式）。

    本版多为 1 级，结果固定 +2；保留公式以便日后升级。
    """
    return 2 + (max(等级, 1) - 1) // 4


# ---------------------------------------------------------------------------
# 公共父类：所有参战者共享的「战斗最小子集」
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class 参战者:
    """一切参战者的公共基类，等价于 docs/原始数据.md 第 3 节的「怪物卡」。

    引擎每回合都要读的字段都在这里：六属性、HP、AC、先攻、区域、攻击、状态，
    以及战斗运行时追加字段（阵营/操控/先攻值/被突袭）。
    子类不再重复这些字段，只追加各自独有的部分。
    """

    # —— 身份 ——
    id: str
    名字: str

    # —— 六项属性（只存原值，调整值现算；怪物可不填，默认 10 即 0 调整值）——
    力量: int = 10
    敏捷: int = 10
    体质: int = 10
    智力: int = 10
    感知: int = 10
    魅力: int = 10

    # —— 战斗数值（每回合都读）——
    当前HP: int = 1
    最大HP: int = 1
    AC: int = 10
    先攻调整值: int = 0
    当前区域: str = "前排"
    存活状态: 存活状态 = 存活状态.正常

    # —— 攻击手段与当前状态 ——
    攻击: list[攻击手段] = field(default_factory=list)
    状态: list[状态效果] = field(default_factory=list)

    # —— 战斗运行时追加字段（不入卡库，战斗结束后丢弃）——
    阵营: 阵营 = 阵营.敌人
    是否玩家控制: bool = False
    操控者: str | None = None        # 玩家 user_id，中断时据此推给正确的人
    先攻值: int | None = None         # 本场掷出的先攻结果，用于排序
    被突袭: bool = False              # True 则跳过自己的第一个回合

    # ---- 派生值（现算，不存）----
    def 调整值(self, which: 属性) -> int:
        """取某项属性的调整值。"""
        return 属性调整值(getattr(self, str(which.value)))

    @property
    def 熟练加值(self) -> int:
        """基类（怪物）无等级概念，固定 +2。子类可覆盖为按等级派生。"""
        return 2

    @property
    def 先攻调整值_有效(self) -> int:
        """先攻调整值；若卡面未给则退化为敏捷调整值。"""
        if self.先攻调整值:
            return self.先攻调整值
        return self.调整值(属性.敏捷)

    # ---- 存活与状态查询 ----
    @property
    def 存活(self) -> bool:
        return self.存活状态 == 存活状态.正常 and self.当前HP > 0

    def 拥有状态(self, 状态: 状态类型) -> bool:
        return any(s.状态 == 状态 and not s.已过期 for s in self.状态)

    # ---- 数值结算（引擎调用，保持不变量）----
    def 受伤(self, 伤害: int) -> int:
        """扣血并钳制到 [0, 最大HP]，归零即倒下。返回实际扣除值。"""
        伤害 = max(0, 伤害)
        旧 = self.当前HP
        self.当前HP = max(0, self.当前HP - 伤害)
        if self.当前HP <= 0:
            self.存活状态 = 存活状态.倒下
        return 旧 - self.当前HP

    def 治疗(self, 治疗量: int) -> int:
        """回血，不超过最大 HP；死亡（倒下）者不因治疗复活。返回实际恢复值。"""
        if not self.存活:
            return 0
        旧 = self.当前HP
        self.当前HP = min(self.最大HP, self.当前HP + max(0, 治疗量))
        return self.当前HP - 旧

    def 添加状态(self, 效果: 状态效果) -> None:
        """加一条状态；同名状态刷新为较长的剩余回合。"""
        for s in self.状态:
            if s.状态 == 效果.状态:
                s.剩余回合 = max(s.剩余回合, 效果.剩余回合)
                s.数值 = max(s.数值, 效果.数值)
                return
        self.状态.append(效果)

    def 递减状态(self) -> list[状态效果]:
        """回合开始：所有状态剩余回合 -1，移除过期项。返回被移除的状态。"""
        for s in self.状态:
            s.剩余回合 -= 1
        过期 = [s for s in self.状态 if s.已过期]
        self.状态 = [s for s in self.状态 if not s.已过期]
        return 过期

    # ---- 序列化 ----
    def _基础卡面(self) -> dict:
        return {
            "id": self.id,
            "名字": self.名字,
            "力量": self.力量, "敏捷": self.敏捷, "体质": self.体质,
            "智力": self.智力, "感知": self.感知, "魅力": self.魅力,
            "当前HP": self.当前HP, "最大HP": self.最大HP,
            "AC": self.AC,
            "先攻调整值": self.先攻调整值,
            "当前区域": self.当前区域,
            "存活状态": self.存活状态.value,
            "攻击": [a.to_dict() for a in self.攻击],
            "状态": [s.to_dict() for s in self.状态],
        }

    def to_card(self) -> dict:
        """导出卡面字典（中文键）。子类追加自己的字段。"""
        return self._基础卡面()

    @staticmethod
    def _解析公共字段(data: dict) -> dict:
        """把卡面字典里公共部分解析成构造参数。"""
        return {
            "id": data["id"],
            "名字": data.get("名字", data["id"]),
            "力量": int(data.get("力量", 10)),
            "敏捷": int(data.get("敏捷", 10)),
            "体质": int(data.get("体质", 10)),
            "智力": int(data.get("智力", 10)),
            "感知": int(data.get("感知", 10)),
            "魅力": int(data.get("魅力", 10)),
            "当前HP": int(data.get("当前HP", data.get("最大HP", 1))),
            "最大HP": int(data.get("最大HP", data.get("当前HP", 1))),
            "AC": int(data.get("AC", 10)),
            "先攻调整值": int(data.get("先攻调整值", 0)),
            "当前区域": data.get("当前区域", "前排"),
            "存活状态": 存活状态(data.get("存活状态", 存活状态.正常)),
            "攻击": [攻击手段.from_dict(a) for a in data.get("攻击", [])],
            "状态": [状态效果.from_dict(s) for s in data.get("状态", [])],
        }


# ---------------------------------------------------------------------------
# 怪物：参战者的子集，无需追加字段，只改默认运行时取向
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class 怪物(参战者):
    """怪物卡 = 角色卡的子集（无背包/升级/社交熟练）。

    默认敌人阵营、DM 操控；「这回合怎么打」由 DM 决定，命中/伤害/HP 全由引擎结算。
    """

    阵营: 阵营 = 阵营.敌人
    是否玩家控制: bool = False

    @classmethod
    def from_card(cls, data: dict) -> "怪物":
        return cls(**参战者._解析公共字段(data))


# ---------------------------------------------------------------------------
# 角色：在公共父类之上追加「完整卡面」字段（玩家与 NPC 共享）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class 角色(参战者):
    """拥有完整卡面的人形参战者：玩家角色与 NPC 的公共父类。

    在 `参战者` 之上追加身份、熟练、技能、背包等字段——这些是怪物没有的部分，
    因此放在这一层，避免怪物白白携带。
    """

    # —— 身份与外观 ——
    种族: str | None = None
    职业: str | None = None
    等级: int = 1
    简介: str | None = None

    # —— 熟练项 ——
    熟练豁免: list[str] = field(default_factory=list)
    熟练技能: list[str] = field(default_factory=list)

    # —— 技能 / 背包 ——
    已学技能: list[已学技能] = field(default_factory=list)
    背包: list[背包道具] = field(default_factory=list)

    @property
    def 熟练加值(self) -> int:
        """覆盖基类：按等级派生。"""
        return 等级熟练加值(self.等级)

    def 豁免熟练(self, which: 属性) -> bool:
        return str(which.value) in self.熟练豁免

    def to_card(self) -> dict:
        卡面 = self._基础卡面()
        卡面.update({
            "种族": self.种族,
            "职业": self.职业,
            "等级": self.等级,
            "简介": self.简介,
            "熟练豁免": list(self.熟练豁免),
            "熟练技能": list(self.熟练技能),
            "已学技能": [s.to_dict() for s in self.已学技能],
            "背包": [i.to_dict() for i in self.背包],
        })
        return 卡面

    @staticmethod
    def _解析角色字段(data: dict) -> dict:
        参数 = 参战者._解析公共字段(data)
        参数.update({
            "种族": data.get("种族"),
            "职业": data.get("职业"),
            "等级": int(data.get("等级", 1)),
            "简介": data.get("简介"),
            "熟练豁免": list(data.get("熟练豁免", [])),
            "熟练技能": list(data.get("熟练技能", [])),
            "已学技能": [已学技能.from_dict(s) for s in data.get("已学技能", [])],
            "背包": [背包道具.from_dict(i) for i in data.get("背包", [])],
        })
        return 参数

    @classmethod
    def from_card(cls, data: dict) -> "角色":
        return cls(**cls._解析角色字段(data))


@dataclass(slots=True)
class 玩家角色(角色):
    """玩家操控的冒险者：先攻/攻击/伤害/豁免靠 interrupt 报骰。"""

    阵营: 阵营 = 阵营.玩家
    是否玩家控制: bool = True

    @classmethod
    def from_card(cls, data: dict) -> "玩家角色":
        return cls(**cls._解析角色字段(data))


@dataclass(slots=True)
class 非玩家角色(角色):
    """NPC：与玩家角色共享完整卡面，但由 DM 操控、引擎自动掷骰。

    阵营默认友方（玩家方），可在创建时改为敌人；与「怪物」的区别在于拥有完整卡面。
    """

    阵营: 阵营 = 阵营.玩家
    是否玩家控制: bool = False

    @classmethod
    def from_card(cls, data: dict) -> "非玩家角色":
        return cls(**cls._解析角色字段(data))
