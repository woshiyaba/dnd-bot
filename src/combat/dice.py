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
_骰子表达式 = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)
_纯常量 = re.compile(r"^\s*([+-]?\d+)\s*$")


@dataclass(slots=True)
class 掷骰结果:
    """一次掷骰的明细，便于日志与叙述。"""

    表达式: str
    骰数: int          # 实际掷出的每颗骰值
    各骰: list[int]
    修正值: int
    总和: int

    def __str__(self) -> str:
        细节 = "+".join(str(x) for x in self.各骰) or "0"
        if self.修正值:
            细节 += f"{self.修正值:+d}"
        return f"{self.表达式} → [{细节}] = {self.总和}"


class 骰子:
    """带可复现随机源的掷骰器。同一 seed 产出同一序列，便于回放与测试。"""

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def d20(self) -> int:
        """掷一颗 d20，返回 1–20 原始值。"""
        return self._rng.randint(1, 20)

    def 掷(self, 表达式: str, *, 重击: bool = False) -> 掷骰结果:
        """按表达式掷骰。

        重击=True 时骰子数量翻倍（修正值不翻倍），对应规则「重击伤害骰翻倍」。
        """
        表达式 = str(表达式).strip()

        常量匹配 = _纯常量.match(表达式)
        if 常量匹配:
            值 = int(常量匹配.group(1))
            return 掷骰结果(表达式=表达式, 骰数=0, 各骰=[], 修正值=值, 总和=值)

        匹配 = _骰子表达式.match(表达式)
        if not 匹配:
            raise ValueError(f"无法解析的骰子表达式：{表达式!r}")

        骰数 = int(匹配.group(1) or 1)
        面数 = int(匹配.group(2))
        修正值 = int((匹配.group(3) or "0").replace(" ", "")) if 匹配.group(3) else 0
        if 重击:
            骰数 *= 2

        各骰 = [self._rng.randint(1, 面数) for _ in range(骰数)]
        总和 = sum(各骰) + 修正值
        return 掷骰结果(表达式=表达式, 骰数=骰数, 各骰=各骰, 修正值=修正值, 总和=总和)


def 解析骰子(表达式: str) -> tuple[int, int, int]:
    """把表达式解析为 (骰数, 面数, 修正值)，纯常量返回 (0, 0, 值)。仅做校验/展示用。"""
    表达式 = str(表达式).strip()
    常量匹配 = _纯常量.match(表达式)
    if 常量匹配:
        return 0, 0, int(常量匹配.group(1))
    匹配 = _骰子表达式.match(表达式)
    if not 匹配:
        raise ValueError(f"无法解析的骰子表达式：{表达式!r}")
    修正值 = int((匹配.group(3) or "0").replace(" ", "")) if 匹配.group(3) else 0
    return int(匹配.group(1) or 1), int(匹配.group(2)), 修正值
