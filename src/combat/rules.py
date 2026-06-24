"""战斗规则结算（纯函数，不碰图状态）。

集中实现 docs/原始数据.md 第 0 节「全局约定」里那条核心判定：
``d20 + 属性调整值 (+熟练加值，若熟练)`` 对比 ``DC / AC``，达到或超过即成功。
攻击检定额外处理重击（d20=20 必中且伤害翻倍）与必失（d20=1）。

规则归引擎：这里只做确定性数学，不掷骰（骰值由调用方提供：玩家中断报数 / 引擎自动掷）。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.model.combatant import 参战者, 角色
from src.model.enums import 属性


@dataclass(slots=True)
class 攻击判定:
    """一次攻击检定的结果。"""

    命中: bool
    重击: bool
    必失: bool
    d20: int
    命中加值: int
    目标AC: int

    @property
    def 总值(self) -> int:
        return self.d20 + self.命中加值


def 判定攻击(d20: int, 命中加值: int, 目标AC: int) -> 攻击判定:
    """攻击检定。

    - d20 == 20：必中且重击，无视调整值与 AC。
    - d20 == 1：必失，无视调整值与 AC。
    - 否则：``d20 + 命中加值 >= 目标AC`` 即命中。
    """
    if d20 >= 20:
        return 攻击判定(命中=True, 重击=True, 必失=False, d20=d20, 命中加值=命中加值, 目标AC=目标AC)
    if d20 <= 1:
        return 攻击判定(命中=False, 重击=False, 必失=True, d20=d20, 命中加值=命中加值, 目标AC=目标AC)
    命中 = (d20 + 命中加值) >= 目标AC
    return 攻击判定(命中=命中, 重击=False, 必失=False, d20=d20, 命中加值=命中加值, 目标AC=目标AC)


def 豁免加值(who: 参战者, 属性项: 属性) -> int:
    """豁免/检定的固定加值：属性调整值 (+熟练加值，若该项熟练)。

    熟练仅角色（含 NPC）有熟练豁免列表；怪物按属性调整值算。
    """
    加值 = who.调整值(属性项)
    if isinstance(who, 角色) and who.豁免熟练(属性项):
        加值 += who.熟练加值
    return 加值


def 技能加值(who: 参战者, 属性项: 属性, *, 熟练: bool = False) -> int:
    """属性检定加值：属性调整值 (+熟练加值，若熟练)。"""
    加值 = who.调整值(属性项)
    if 熟练:
        加值 += who.熟练加值
    return 加值


def 判定检定(d20: int, 加值: int, DC: int) -> bool:
    """通用检定/豁免：``d20 + 加值 >= DC`` 即成功。"""
    return (d20 + 加值) >= DC


def 够得着(行动者: 参战者, 目标: 参战者, 是远程: bool) -> bool:
    """区域粒度的射程判断。

    近战：必须同区域；远程：不同区域也可（本版不做最大射程衰减）。
    """
    if 是远程:
        return True
    return 行动者.当前区域 == 目标.当前区域
