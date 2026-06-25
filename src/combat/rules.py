"""战斗规则结算（纯函数，不碰图状态）。

集中实现 docs/原始数据.md 第 0 节「全局约定」里那条核心判定：
``d20 + 属性调整值 (+熟练加值，若熟练)`` 对比 ``DC / AC``，达到或超过即成功。
攻击检定额外处理重击（d20=20 必中且伤害翻倍）与必失（d20=1）。

规则归引擎：这里只做确定性数学，不掷骰（骰值由调用方提供：玩家中断报数 / 引擎自动掷）。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.combatant import Character, Combatant
from src.model.enums import Ability


@dataclass(slots=True)
class AttackResult:
    """一次攻击检定的结果。"""

    hit: bool          # 命中
    crit: bool         # 重击
    fumble: bool       # 必失
    d20: int           # d20 原始值
    attack_bonus: int  # 命中加值
    target_ac: int     # 目标 AC

    @property
    def total(self) -> int:
        """命中总值 = d20 + 命中加值。"""
        return self.d20 + self.attack_bonus


def resolve_attack(d20: int, attack_bonus: int, target_ac: int) -> AttackResult:
    """攻击检定。

    - d20 == 20：必中且重击，无视调整值与 AC。
    - d20 == 1：必失，无视调整值与 AC。
    - 否则：``d20 + 命中加值 >= 目标AC`` 即命中。
    """
    if d20 >= 20:
        return AttackResult(hit=True, crit=True, fumble=False, d20=d20, attack_bonus=attack_bonus, target_ac=target_ac)
    if d20 <= 1:
        return AttackResult(hit=False, crit=False, fumble=True, d20=d20, attack_bonus=attack_bonus, target_ac=target_ac)
    hit = (d20 + attack_bonus) >= target_ac
    return AttackResult(hit=hit, crit=False, fumble=False, d20=d20, attack_bonus=attack_bonus, target_ac=target_ac)


def saving_throw_bonus(who: Combatant, ability: Ability) -> int:
    """豁免/检定的固定加值：属性调整值 (+熟练加值，若该项熟练)。

    熟练仅角色（含 NPC）有熟练豁免列表；怪物按属性调整值算。
    """
    bonus = who.modifier(ability)
    if isinstance(who, Character) and who.is_save_proficient(ability):
        bonus += who.proficiency_bonus
    return bonus


def ability_check_bonus(who: Combatant, ability: Ability, *, proficient: bool = False) -> int:
    """属性检定加值：属性调整值 (+熟练加值，若熟练)。"""
    bonus = who.modifier(ability)
    if proficient:
        bonus += who.proficiency_bonus
    return bonus


def check_success(d20: int, bonus: int, dc: int) -> bool:
    """通用检定/豁免：``d20 + 加值 >= DC`` 即成功。"""
    return (d20 + bonus) >= dc


def in_reach(actor: Combatant, target: Combatant, is_ranged: bool) -> bool:
    """区域粒度的射程判断。

    近战：必须同区域；远程：不同区域也可（本版不做最大射程衰减）。
    """
    if is_ranged:
        return True
    return actor.current_zone == target.current_zone
