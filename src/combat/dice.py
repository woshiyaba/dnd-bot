"""骰子工具。

怪物与环境的骰子由引擎自动掷（用可复现的随机源）；
玩家的骰子靠中断收集，不走这里。

支持骰子表达式：``NdM``、``NdM+K``、``dM``（N 默认 1）、纯整数常量。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# NdM(+/-K)：N 可省略（默认 1），修正值可省略
_DICE_RE = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)
_CONST_RE = re.compile(r"^\s*([+-]?\d+)\s*$")


@dataclass(slots=True)
class RollResult:
    """一次掷骰的明细，便于日志与叙述。"""

    expression: str   # 表达式
    count: int        # 骰数：实际掷出的骰子颗数
    rolls: list[int]  # 各骰：每颗骰值
    modifier: int     # 修正值
    total: int        # 总和

    def __str__(self) -> str:
        detail = "+".join(str(x) for x in self.rolls) or "0"
        if self.modifier:
            detail += f"{self.modifier:+d}"
        return f"{self.expression} → [{detail}] = {self.total}"


class Dice:
    """带可复现随机源的掷骰器。同一 seed 产出同一序列，便于回放与测试。"""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def d20(self) -> int:
        """掷一颗 d20，返回 1–20 原始值。"""
        return self._rng.randint(1, 20)

    def roll(self, expression: str, *, crit: bool = False) -> RollResult:
        """按表达式掷骰。

        crit=True 时骰子数量翻倍（修正值不翻倍），对应规则「重击伤害骰翻倍」。
        """
        expression = str(expression).strip()

        const_match = _CONST_RE.match(expression)
        if const_match:
            value = int(const_match.group(1))
            return RollResult(expression=expression, count=0, rolls=[], modifier=value, total=value)

        match = _DICE_RE.match(expression)
        if not match:
            raise ValueError(f"无法解析的骰子表达式：{expression!r}")

        count = int(match.group(1) or 1)
        faces = int(match.group(2))
        modifier = int((match.group(3) or "0").replace(" ", "")) if match.group(3) else 0
        if crit:
            count *= 2

        rolls = [self._rng.randint(1, faces) for _ in range(count)]
        total = sum(rolls) + modifier
        return RollResult(expression=expression, count=count, rolls=rolls, modifier=modifier, total=total)


def parse_dice(expression: str) -> tuple[int, int, int]:
    """把表达式解析为 (骰数, 面数, 修正值)，纯常量返回 (0, 0, 值)。仅做校验/展示用。"""
    expression = str(expression).strip()
    const_match = _CONST_RE.match(expression)
    if const_match:
        return 0, 0, int(const_match.group(1))
    match = _DICE_RE.match(expression)
    if not match:
        raise ValueError(f"无法解析的骰子表达式：{expression!r}")
    modifier = int((match.group(3) or "0").replace(" ", "")) if match.group(3) else 0
    return int(match.group(1) or 1), int(match.group(2)), modifier
