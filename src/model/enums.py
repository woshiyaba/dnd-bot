"""战斗领域枚举。

集中定义参战者、攻击、状态、战斗流程相关的封闭花名册。
枚举值即为序列化后写进卡面/状态的字符串，便于直接落库与前端展示。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """值即字符串的枚举基类（Python 3.13 自带 enum.StrEnum，这里显式声明以兼容旧逻辑）。"""

    def __str__(self) -> str:  # pragma: no cover - 仅为可读
        return str(self.value)


class Ability(StrEnum):
    """六项基础属性。检定/攻击/豁免的调整值都由这六项现算。

    成员值必须与 ``Combatant`` 上对应字段名一致，因为 ``modifier`` 用
    ``getattr(self, ability.value)`` 取属性原值。
    """

    STRENGTH = "strength"  # 力量
    DEXTERITY = "dexterity"  # 敏捷
    CONSTITUTION = "constitution"  # 体质
    INTELLIGENCE = "intelligence"  # 智力
    WISDOM = "wisdom"  # 感知
    CHARISMA = "charisma"  # 魅力


class DamageType(StrEnum):
    """伤害类型，本身不带规则，由抗性/易伤等机制参考。"""

    SLASHING = "slashing"  # 挥砍
    PIERCING = "piercing"  # 穿刺
    BLUDGEONING = "bludgeoning"  # 钝击
    ACID = "acid"  # 强酸
    COLD = "cold"  # 冷冻
    FIRE = "fire"  # 火焰
    FORCE = "force"  # 力场
    LIGHTNING = "lightning"  # 闪电
    NECROTIC = "necrotic"  # 黯蚀
    POISON = "poison"  # 毒素
    PSYCHIC = "psychic"  # 心灵
    RADIANT = "radiant"  # 光耀
    THUNDER = "thunder"  # 雷鸣


class Range(StrEnum):
    """攻击射程，配合区域判断够不够得着。"""

    MELEE = "melee"  # 近战
    RANGED = "ranged"  # 远程


class LifeState(StrEnum):
    """本版只区分能否继续行动；不做死亡豁免。"""

    ALIVE = "alive"  # 正常
    DOWN = "down"  # 倒下


class ConditionType(StrEnum):
    """起步支持的状态枚举，机械效果见 docs/原始数据.md 1.8。"""

    PRONE = "prone"  # 倒地
    POISONED = "poisoned"  # 中毒
    RESTRAINED = "restrained"  # 束缚
    STUNNED = "stunned"  # 眩晕
    DAMAGE_OVER_TIME = "damage_over_time"  # 持续伤害


class Faction(StrEnum):
    """判胜负用的阵营划分。"""

    PLAYER = "player"  # 玩家
    ENEMY = "enemy"  # 敌人


class CombatPhase(StrEnum):
    """供调试与前端展示当前处于流程的哪一步。"""

    SETUP = "setup"  # 初始化
    SURPRISE = "surprise"  # 判突袭
    INITIATIVE = "initiative"  # 掷先攻
    IN_TURN = "in_turn"  # 回合中
    SETTLEMENT = "settlement"  # 结算
    ENDED = "ended"  # 结束


class CombatOutcome(StrEnum):
    """条件边据此决定走"下一位"还是"结算"。"""

    ONGOING = "ongoing"  # 进行中
    PLAYERS_WIN = "players_win"  # 玩家胜
    PLAYERS_LOSE = "players_lose"  # 玩家败


class ActionType(StrEnum):
    """声明行动节点产出的动作种类。"""

    ATTACK = "attack"  # 攻击
    SKILL = "skill"  # 技能
    ITEM = "item"  # 道具
    IMPROVISE = "improvise"  # 创意
    MOVE = "move"  # 移动
    PASS = "pass"  # 放弃


class InterruptType(StrEnum):
    """需要玩家报骰/选择的中断点，见 docs/战斗/03-中断交互协议.md。"""

    ROLL_INITIATIVE = "roll_initiative"  # 掷先攻
    DECLARE_ACTION = "declare_action"  # 声明行动
    ATTACK_ROLL = "attack_roll"  # 攻击检定
    DAMAGE_ROLL = "damage_roll"  # 伤害掷骰
    SAVING_THROW = "saving_throw"  # 豁免检定
    ABILITY_CHECK = "ability_check"  # 属性检定
