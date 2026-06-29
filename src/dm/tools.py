"""暴露给 DM（LLM）的 LangChain 工具：骰子 + 知识库查阅。

设计要点：
- **骰子工具复用引擎的可复现骰子**，但本包不反向 import ``src.combat``：
  战斗层在装配时调用 :func:`set_dice_provider` 注入一个"取当前引擎骰子"的回调，
  工具每次掷骰都通过它拿到**活的**骰子实例，因而与引擎共享同一可复现随机序列
  （同 ``random_seed`` 可回放），DM 智能体又能安全地缓存复用。
- **边界**：这些骰子只服务于突袭对抗、即兴 DC、怪物决策随机性、纯叙事检定；
  命中/伤害/扣血仍由引擎在 ``rules.py`` 结算，提示词里也会明确告诉 DM。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import tool

from src.dm.knowledge import get_registry

# 取"引擎当前骰子"的回调，由战斗层通过 set_dice_provider 注入
_dice_provider: Callable[[], Any] | None = None


def set_dice_provider(provider: Callable[[], Any]) -> None:
    """注入一个返回"引擎当前骰子"的回调（战斗层调用）。

    provider 应返回一个具备 ``roll(expression)`` 方法的对象（即 ``src.combat.dice.Dice``）。
    传入回调而非实例，避免引擎重置随机种子后工具仍握着旧骰子。
    """
    global _dice_provider
    _dice_provider = provider


def _dice() -> Any:
    """取当前可掷骰对象：优先用注入的引擎骰子，否则惰性兜底一个独立骰子。

    兜底分支用运行期局部 import，避免本模块在顶层反向依赖 ``src.combat``。
    """
    if _dice_provider is not None:
        return _dice_provider()
    from src.combat.dice import current_engine_dice  # 运行期兜底，非顶层依赖

    return current_engine_dice()


def _roll(expression: str) -> dict:
    """按表达式掷骰并返回结构化结果（点数明细 + 总和），便于 DM 引用与日志。"""
    result = _dice().roll(expression)
    return {
        "expression": result.expression,
        "rolls": result.rolls,
        "modifier": result.modifier,
        "total": result.total,
    }


# ---------------------------------------------------------------------------
# 骰子工具：固定面数 d4–d20 + 通用表达式
# ---------------------------------------------------------------------------
@tool
def roll_d4() -> dict:
    """掷一颗 d4（四面骰），返回点数与总和。"""
    return _roll("1d4")


@tool
def roll_d6() -> dict:
    """掷一颗 d6（六面骰），返回点数与总和。"""
    return _roll("1d6")


@tool
def roll_d8() -> dict:
    """掷一颗 d8（八面骰），返回点数与总和。"""
    return _roll("1d8")


@tool
def roll_d10() -> dict:
    """掷一颗 d10（十面骰），返回点数与总和。"""
    return _roll("1d10")


@tool
def roll_d12() -> dict:
    """掷一颗 d12（十二面骰），返回点数与总和。"""
    return _roll("1d12")


@tool
def roll_d20() -> dict:
    """掷一颗 d20（二十面骰），返回点数与总和。用于检定/豁免/对抗的基础骰。"""
    return _roll("1d20")


@tool
def roll_expr(expression: str) -> dict:
    """按骰子表达式掷骰，支持 ``NdM``、``NdM+K``、``dM`` 与纯整数常量（如 ``2d6+3``、``4d10``）。

    用于即兴伤害、多骰检定等。无法解析时返回 ``{"error": ...}``。
    """
    try:
        return _roll(expression)
    except (ValueError, TypeError) as exc:
        return {"error": f"无法解析的骰子表达式：{expression!r}（{exc}）"}


# ---------------------------------------------------------------------------
# 知识库工具：检索目录 + 读取正文
# ---------------------------------------------------------------------------
@tool
def kb_search(query: str, category: str = "") -> list[dict]:
    """检索知识库目录，返回匹配的文档摘要列表（不含正文）。

    参数:
        query: 关键词，匹配文档 id/名称/摘要/标签的子串；空字符串返回全部。
        category: 可选分类过滤，如 ``rule`` / ``monster`` / ``skill``；留空不过滤。
    返回:
        目录项列表，每项含 ``doc_id / name / category / description``。
        确认要用某条后，再用 ``kb_read`` 读它的正文。
    """
    return get_registry().search(query, category or None)


@tool
def kb_read(doc_id: str) -> str:
    """读取某知识库文档的完整正文。

    参数:
        doc_id: 文档 id（即文件名去扩展名，可先用 ``kb_search`` 查到）。
    返回:
        正文 Markdown 文本；找不到时返回可用 id 的提示。
    """
    return get_registry().read(doc_id)


# 分组导出，供 agent 装配
DICE_TOOLS = [roll_d4, roll_d6, roll_d8, roll_d10, roll_d12, roll_d20, roll_expr]
KB_TOOLS = [kb_search, kb_read]
ALL_DM_TOOLS = [*DICE_TOOLS, *KB_TOOLS]
