"""参战者数据模型（继承体系）。

设计目标（按用户要求「尽量抽象」）：怪物 / 角色 / NPC 的**通用数据**只定义一次，
放在公共父类 `Combatant`；各自**独有**的字段由子类追加，绝不重复定义。

继承关系::

    Combatant（参战者）               # 引擎每回合都要读的最小子集 == 怪物卡
    ├── Monster（怪物）               # 就是参战者本身，默认敌人阵营、DM 操控
    └── Character（角色）             # 追加完整卡面：种族/职业/熟练/技能/背包
        ├── PlayerCharacter（玩家角色） # 玩家操控，先攻/攻击/豁免靠中断报骰
        └── NPC（非玩家角色）          # 同样的完整卡面，但由 DM 操控

字段沿用 docs/原始数据.md 的卡面（英文键），便于直接从卡面 JSON 加载、写回世界库。
派生值（属性调整值、熟练加值）一律现算，不入卡。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.model.attack import Attack
from src.model.effects import Condition, InventoryItem, LearnedSkill
from src.model.enums import Ability, Faction, LifeState, ConditionType


def ability_modifier(score: int) -> int:
    """属性调整值：`(属性值 − 10) ÷ 2`，向下取整。负数也正确向下取整。"""
    return (score - 10) // 2


def proficiency_bonus_for_level(level: int) -> int:
    """由等级派生熟练加值：1 级 +2，之后每 4 级 +1（5e 通用公式）。

    本版多为 1 级，结果固定 +2；保留公式以便日后升级。
    """
    return 2 + (max(level, 1) - 1) // 4


# ---------------------------------------------------------------------------
# 公共父类：所有参战者共享的「战斗最小子集」
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Combatant:
    """一切参战者的公共基类，等价于 docs/原始数据.md 第 3 节的「怪物卡」。

    引擎每回合都要读的字段都在这里：六属性、HP、AC、先攻、区域、攻击、状态，
    以及战斗运行时追加字段（阵营/操控/先攻值/被突袭）。
    子类不再重复这些字段，只追加各自独有的部分。
    """

    # —— 身份 ——
    id: str
    name: str  # 名字

    # —— 六项属性（只存原值，调整值现算；怪物可不填，默认 10 即 0 调整值）——
    # 字段名必须与 Ability 成员值一致（modifier 用 getattr 取值）。
    strength: int = 10  # 力量
    dexterity: int = 10  # 敏捷
    constitution: int = 10  # 体质
    intelligence: int = 10  # 智力
    wisdom: int = 10  # 感知
    charisma: int = 10  # 魅力

    # —— 战斗数值（每回合都读）——
    current_hp: int = 1  # 当前 HP
    max_hp: int = 1  # 最大 HP
    ac: int = 10  # 护甲等级
    initiative_bonus: int = 0  # 先攻调整值
    current_zone: str = "前排"  # 当前区域
    life_state: LifeState = LifeState.ALIVE  # 存活状态

    # —— 攻击手段与当前状态 ——
    attacks: list[Attack] = field(default_factory=list)  # 攻击
    conditions: list[Condition] = field(default_factory=list)  # 状态

    # —— 战斗运行时追加字段（不入卡库，战斗结束后丢弃）——
    faction: Faction = Faction.ENEMY  # 阵营
    is_player_controlled: bool = False  # 是否玩家控制
    controller: str | None = None  # 操控者：玩家 user_id，中断时据此推给正确的人
    initiative: int | None = None  # 先攻值：本场掷出的先攻结果，用于排序
    is_surprised: bool = False  # 被突袭：True 则跳过自己的第一个回合

    # ---- 派生值（现算，不存）----
    def modifier(self, ability: Ability) -> int:
        """取某项属性的调整值。"""
        return ability_modifier(getattr(self, str(ability.value)))

    @property
    def proficiency_bonus(self) -> int:
        """熟练加值。基类（怪物）无等级概念，固定 +2；子类可覆盖为按等级派生。"""
        return 2

    @property
    def effective_initiative_bonus(self) -> int:
        """有效先攻调整值；若卡面未给则退化为敏捷调整值。"""
        if self.initiative_bonus:
            return self.initiative_bonus
        return self.modifier(Ability.DEXTERITY)

    # ---- 存活与状态查询 ----
    @property
    def is_alive(self) -> bool:
        """是否存活。"""
        return self.life_state == LifeState.ALIVE and self.current_hp > 0

    def has_condition(self, condition_type: ConditionType) -> bool:
        """是否拥有某状态（且未过期）。"""
        return any(
            s.kind == condition_type and not s.is_expired for s in self.conditions
        )

    # ---- 数值结算（引擎调用，保持不变量）----
    def take_damage(self, amount: int) -> int:
        """受伤：扣血并钳制到 [0, 最大HP]，归零即倒下。返回实际扣除值。"""
        amount = max(0, amount)
        old = self.current_hp
        self.current_hp = max(0, self.current_hp - amount)
        if self.current_hp <= 0:
            self.life_state = LifeState.DOWN
        return old - self.current_hp

    def heal(self, amount: int) -> int:
        """治疗：回血，不超过最大 HP；死亡（倒下）者不因治疗复活。返回实际恢复值。"""
        if not self.is_alive:
            return 0
        old = self.current_hp
        self.current_hp = min(self.max_hp, self.current_hp + max(0, amount))
        return self.current_hp - old

    def add_condition(self, effect: Condition) -> None:
        """添加状态：同名状态刷新为较长的剩余回合。"""
        for s in self.conditions:
            if s.kind == effect.kind:
                s.rounds_left = max(s.rounds_left, effect.rounds_left)
                s.amount = max(s.amount, effect.amount)
                return
        self.conditions.append(effect)

    def tick_conditions(self) -> list[Condition]:
        """递减状态：回合开始时所有状态剩余回合 -1，移除过期项。返回被移除的状态。"""
        for s in self.conditions:
            s.rounds_left -= 1
        expired = [s for s in self.conditions if s.is_expired]
        self.conditions = [s for s in self.conditions if not s.is_expired]
        return expired

    # ---- 序列化 ----
    def _base_card(self) -> dict:
        """公共卡面字段（英文键）。"""
        return {
            "id": self.id,
            "name": self.name,
            "strength": self.strength,
            "dexterity": self.dexterity,
            "constitution": self.constitution,
            "intelligence": self.intelligence,
            "wisdom": self.wisdom,
            "charisma": self.charisma,
            "current_hp": self.current_hp,
            "max_hp": self.max_hp,
            "ac": self.ac,
            "initiative_bonus": self.initiative_bonus,
            "current_zone": self.current_zone,
            "life_state": self.life_state.value,
            "attacks": [a.to_dict() for a in self.attacks],
            "conditions": [s.to_dict() for s in self.conditions],
        }

    def to_card(self) -> dict:
        """导出卡面字典（英文键）。子类追加自己的字段。"""
        return self._base_card()

    @staticmethod
    def _parse_common_fields(data: dict) -> dict:
        """把卡面字典里公共部分解析成构造参数。"""
        return {
            "id": data["id"],
            "name": data.get("name", data["id"]),
            "strength": int(data.get("strength", 10)),
            "dexterity": int(data.get("dexterity", 10)),
            "constitution": int(data.get("constitution", 10)),
            "intelligence": int(data.get("intelligence", 10)),
            "wisdom": int(data.get("wisdom", 10)),
            "charisma": int(data.get("charisma", 10)),
            "current_hp": int(data.get("current_hp", data.get("max_hp", 1))),
            "max_hp": int(data.get("max_hp", data.get("current_hp", 1))),
            "ac": int(data.get("ac", 10)),
            "initiative_bonus": int(data.get("initiative_bonus", 0)),
            "current_zone": data.get("current_zone", "前排"),
            "life_state": LifeState(data.get("life_state", LifeState.ALIVE)),
            "attacks": [Attack.from_dict(a) for a in data.get("attacks", [])],
            "conditions": [Condition.from_dict(s) for s in data.get("conditions", [])],
        }


# ---------------------------------------------------------------------------
# 怪物：参战者的子集，无需追加字段，只改默认运行时取向
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Monster(Combatant):
    """怪物卡 = 角色卡的子集（无背包/升级/社交熟练）。

    默认敌人阵营、DM 操控；「这回合怎么打」由 DM 决定，命中/伤害/HP 全由引擎结算。
    """

    faction: Faction = Faction.ENEMY  # 阵营
    is_player_controlled: bool = False  # 是否玩家控制

    @classmethod
    def from_card(cls, data: dict) -> "Monster":
        """从卡面字典构造怪物。"""
        return cls(**Combatant._parse_common_fields(data))


# ---------------------------------------------------------------------------
# 角色：在公共父类之上追加「完整卡面」字段（玩家与 NPC 共享）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Character(Combatant):
    """拥有完整卡面的人形参战者：玩家角色与 NPC 的公共父类。

    在 `Combatant` 之上追加身份、熟练、技能、背包等字段——这些是怪物没有的部分，
    因此放在这一层，避免怪物白白携带。
    """

    # —— 身份与外观 ——
    race: str | None = None  # 种族
    char_class: str | None = None  # 职业
    level: int = 1  # 等级
    bio: str | None = None  # 简介

    # —— 熟练项 ——
    save_proficiencies: list[str] = field(
        default_factory=list
    )  # 熟练豁免（存 Ability 值）
    skill_proficiencies: list[str] = field(default_factory=list)  # 熟练技能

    # —— 技能 / 背包 ——
    skills: list[LearnedSkill] = field(default_factory=list)  # 已学技能
    inventory: list[InventoryItem] = field(default_factory=list)  # 背包

    @property
    def proficiency_bonus(self) -> int:
        """覆盖基类：按等级派生熟练加值。"""
        return proficiency_bonus_for_level(self.level)

    def is_save_proficient(self, ability: Ability) -> bool:
        """该属性是否豁免熟练。"""
        return str(ability.value) in self.save_proficiencies

    def to_card(self) -> dict:
        """导出完整角色卡面（英文键）。"""
        card = self._base_card()
        card.update(
            {
                "race": self.race,
                "char_class": self.char_class,
                "level": self.level,
                "bio": self.bio,
                "save_proficiencies": list(self.save_proficiencies),
                "skill_proficiencies": list(self.skill_proficiencies),
                "skills": [s.to_dict() for s in self.skills],
                "inventory": [i.to_dict() for i in self.inventory],
            }
        )
        return card

    @staticmethod
    def _parse_character_fields(data: dict) -> dict:
        """把完整角色卡面解析成构造参数。"""
        params = Combatant._parse_common_fields(data)
        params.update(
            {
                "race": data.get("race"),
                "char_class": data.get("char_class"),
                "level": int(data.get("level", 1)),
                "bio": data.get("bio"),
                "save_proficiencies": list(data.get("save_proficiencies", [])),
                "skill_proficiencies": list(data.get("skill_proficiencies", [])),
                "skills": [LearnedSkill.from_dict(s) for s in data.get("skills", [])],
                "inventory": [
                    InventoryItem.from_dict(i) for i in data.get("inventory", [])
                ],
            }
        )
        return params

    @classmethod
    def from_card(cls, data: dict) -> "Character":
        """从卡面字典构造角色。"""
        return cls(**cls._parse_character_fields(data))


@dataclass(slots=True)
class PlayerCharacter(Character):
    """玩家操控的冒险者：先攻/攻击/伤害/豁免靠 interrupt 报骰。"""

    faction: Faction = Faction.PLAYER  # 阵营
    is_player_controlled: bool = True  # 是否玩家控制

    @classmethod
    def from_card(cls, data: dict) -> "PlayerCharacter":
        """从卡面字典构造玩家角色。"""
        return cls(**cls._parse_character_fields(data))


@dataclass(slots=True)
class NPC(Character):
    """NPC：与玩家角色共享完整卡面，但由 DM 操控、引擎自动掷骰。

    阵营默认友方（玩家方），可在创建时改为敌人；与「怪物」的区别在于拥有完整卡面。
    """

    faction: Faction = Faction.PLAYER  # 阵营
    is_player_controlled: bool = False  # 是否玩家控制

    @classmethod
    def from_card(cls, data: dict) -> "NPC":
        """从卡面字典构造 NPC。"""
        return cls(**cls._parse_character_fields(data))
