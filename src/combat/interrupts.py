"""中断交互协议（骰子交给玩家）。

实现 docs/战斗/03-中断交互协议.md：构造「图 → 前端」的中断请求负载，
以及构造「声明行动」节点要推给玩家的合法选项。恢复值由前端按文档格式回报，
各节点自行读取 `Command(resume=...)` 的字典，本模块只负责出请求与做范围校验。
"""

from __future__ import annotations

from typing import Any

from src.combat.rules import 够得着
from src.model.combatant import 参战者, 角色
from src.model.enums import 中断类型


def 构造中断请求(
    *,
    类型: 中断类型,
    对象: 参战者,
    提示: str,
    需要骰子: str | None = None,
    加值: int = 0,
    选项: dict | None = None,
    附带: dict | None = None,
    期望返回: dict | None = None,
) -> dict[str, Any]:
    """统一的中断请求负载（见文档第 2 节）。

    `面向.user_id` = 操控者，前端据此把「该谁掷什么」推给正确的人。
    """
    请求: dict[str, Any] = {
        "中断类型": str(类型.value),
        "面向": {
            "combatant_id": 对象.id,
            "user_id": 对象.操控者,
        },
        "提示": 提示,
        "需要骰子": 需要骰子,
        "加值": 加值,
        "选项": 选项,
        "期望返回": 期望返回 or _默认期望返回(类型),
    }
    if 附带:
        请求["附带"] = 附带
    return 请求


def _默认期望返回(类型: 中断类型) -> dict:
    """各中断类型的恢复值 schema 提示（文档第 3 节）。"""
    if 类型 == 中断类型.伤害掷骰:
        return {"结果": "int 伤害总和"}
    if 类型 == 中断类型.声明行动:
        return {"行动类型": "str", "目标id": "str(可选)"}
    # 掷先攻 / 攻击检定 / 豁免检定 / 属性检定
    return {"d20": "int 1-20 原始值"}


def 构造声明选项(行动者: 参战者, combatants: dict[str, 参战者]) -> dict[str, Any]:
    """为「声明行动」中断构造合法选项（文档 2.1）。

    - 攻击：每件武器列出射程内、存活的敌方目标（按区域过滤）。
    - 技能/道具/移动/创意：仅角色（含 NPC）持有，怪物为空。
    """
    敌方存活 = [
        c for c in combatants.values()
        if c.阵营 != 行动者.阵营 and c.存活
    ]

    攻击项 = []
    for 武器 in 行动者.攻击:
        目标 = [
            {"id": t.id, "名字": t.名字, "区域": t.当前区域}
            for t in 敌方存活
            if 够得着(行动者, t, 武器.是远程)
        ]
        攻击项.append({
            "攻击名": 武器.名字,
            "射程": str(武器.射程.value),
            "目标": 目标,
        })

    选项: dict[str, Any] = {"攻击": 攻击项, "创意": True}

    # 移动：可去的其他区域
    所有区域 = sorted({c.当前区域 for c in combatants.values()})
    选项["移动"] = [
        {"目标区域": 区域} for 区域 in 所有区域 if 区域 != 行动者.当前区域
    ]

    if isinstance(行动者, 角色):
        选项["技能"] = [
            {"技能id": s.技能id, "剩余充能": s.当前充能, "冷却剩余": s.冷却剩余}
            for s in 行动者.已学技能 if s.可用
        ]
        选项["道具"] = [
            {"道具id": i.道具id, "数量": i.数量}
            for i in 行动者.背包 if i.可用
        ]
    return 选项


def 校验d20(恢复值: Any, *, 默认: int = 10) -> int:
    """从恢复值里取 d20 原始值并做 1–20 范围校验（信任边界：加值一律引擎算）。"""
    if isinstance(恢复值, dict):
        原始 = 恢复值.get("d20", 默认)
    else:
        原始 = 恢复值
    try:
        值 = int(原始)
    except (TypeError, ValueError):
        return 默认
    return max(1, min(20, 值))


def 取伤害结果(恢复值: Any) -> int | None:
    """从恢复值里取玩家自报的伤害总和（攻击检定可一次带回 `伤害结果`）。"""
    if not isinstance(恢复值, dict):
        return None
    for 键 in ("伤害结果", "结果"):
        if 键 in 恢复值:
            try:
                return max(0, int(恢复值[键]))
            except (TypeError, ValueError):
                return None
    return None
