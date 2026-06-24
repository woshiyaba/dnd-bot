"""战斗子图的节点实现。

每个节点读写 docs/战斗/01 定义的 `CombatState`，对照 docs/战斗/02 的流程：

    enter_combat → judge_surprise → roll_initiative → next_turn
    → declare_action → resolve_action → narrate → check_end ─┐
              ▲────────────────(战斗结果==进行中)─────────────┘
              └──(否则)──► settle → END

设计原则：**规则归引擎，叙述归 DM，骰子归玩家**。
- 引擎节点 = 纯 Python 确定性结算；
- 玩家骰子 = `interrupt()` 收集（仅 `是否玩家控制` 的参战者）；
- 怪物/环境骰子 = 引擎用可复现随机源自动掷；
- DM 决策/叙述目前为确定性启发式占位（见 `_dm决策` / `narrate`），保留接 LLM 的钩子。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from src.combat.dice import 骰子
from src.combat.interrupts import (
    构造中断请求,
    构造声明选项,
    取伤害结果,
    校验d20,
)
from src.combat.rules import (
    判定攻击,
    判定检定,
    技能加值,
    够得着,
)
from src.model.combat_state import CombatState, 加载参战者表
from src.model.combatant import 参战者, 角色
from src.model.effects import 状态效果
from src.model.enums import (
    中断类型,
    属性,
    战斗结果,
    战斗阶段,
    状态类型,
    行动类型,
    阵营,
)

logger = logging.getLogger(__name__)

# 可复现随机源：怪物/环境骰子走这里。可在 enter_combat 用场景里的「随机种子」重置。
_骰子 = 骰子()


def _重置骰子(seed: int | None) -> None:
    global _骰子
    _骰子 = 骰子(seed)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _当前行动者(state: CombatState) -> 参战者:
    """取先攻指针指向的参战者。"""
    actor_id = state["先攻顺序"][state["当前指针"]]
    return state["combatants"][actor_id]


def _记日志(state: CombatState, 事件: list[dict]) -> list[dict]:
    """把本回合事件追加进全场日志，返回新的日志列表。"""
    日志 = list(state.get("战斗日志", []))
    日志.extend(事件)
    return 日志


def _带轮次(state: CombatState, 事件: dict) -> dict:
    """给事件补上轮次/行动者信息，便于前端回放与 DM 上下文。"""
    事件.setdefault("轮次", state.get("当前轮次"))
    return 事件


# ---------------------------------------------------------------------------
# 1. enter_combat（引擎）
# ---------------------------------------------------------------------------
def enter_combat(state: CombatState) -> dict:
    """初始化战斗：加载参战者、摆好区域、清空工作区。"""
    场景 = state.get("场景上下文", {}) or {}
    if "随机种子" in 场景:
        _重置骰子(int(场景["随机种子"]))

    combatants = state.get("combatants") or 加载参战者表(场景)

    logger.info("[enter_combat] 进入战斗 | 参战者=%d", len(combatants))
    return {
        "combatants": combatants,
        "先攻顺序": [],
        "当前指针": -1,
        "当前轮次": 0,
        "阶段": 战斗阶段.初始化,
        "战斗结果": 战斗结果.进行中,
        "当前行动": None,
        "本回合事件": [],
        "战斗日志": list(state.get("战斗日志", [])),
    }


# ---------------------------------------------------------------------------
# 2. judge_surprise（DM）
# ---------------------------------------------------------------------------
def judge_surprise(state: CombatState) -> dict:
    """判定突袭。

    v0 简化：纯叙事判定，不掷隐匿/察觉。被突袭名单由 `场景上下文["被突袭"]`（id 列表）给出，
    缺省则无人被突袭。日后接 DM（LLM）时在此节点替换为「潜行 vs 被动察觉」对抗。
    """
    场景 = state.get("场景上下文", {}) or {}
    combatants = state["combatants"]
    被突袭 = [cid for cid in 场景.get("被突袭", []) if cid in combatants]
    for cid in 被突袭:
        combatants[cid].被突袭 = True

    事件 = [{"事件": "判定突袭", "被突袭": 被突袭}]
    logger.info("[judge_surprise] 被突袭=%s", 被突袭)
    return {
        "combatants": combatants,
        "阶段": 战斗阶段.判突袭,
        "战斗日志": _记日志(state, 事件),
    }


# ---------------------------------------------------------------------------
# 3. roll_initiative（引擎 + 玩家中断）
# ---------------------------------------------------------------------------
def roll_initiative(state: CombatState) -> dict:
    """掷先攻、排定行动顺序。

    玩家参战者逐个 `interrupt` 报 d20（引擎加先攻调整值）；怪物引擎自动掷。
    多个玩家时按字典顺序逐个中断收集（同一时刻只挂起一个）。
    """
    combatants = state["combatants"]

    for c in combatants.values():
        if c.是否玩家控制:
            提示 = f"轮到 {c.名字}，掷先攻：d20 + {c.先攻调整值_有效}"
            恢复值 = interrupt(构造中断请求(
                类型=中断类型.掷先攻,
                对象=c,
                提示=提示,
                需要骰子="d20",
                加值=c.先攻调整值_有效,
            ))
            d20 = 校验d20(恢复值)
        else:
            d20 = _骰子.d20()
        c.先攻值 = d20 + c.先攻调整值_有效

    # 降序排序；平手用敏捷调整值，再用引擎随机数打破
    顺序 = sorted(
        combatants.values(),
        key=lambda c: (c.先攻值, c.调整值(属性.敏捷), _骰子.d20()),
        reverse=True,
    )
    先攻顺序 = [c.id for c in 顺序]

    事件 = [{"事件": "掷先攻", "先攻顺序": [
        {"id": c.id, "名字": c.名字, "先攻值": c.先攻值} for c in 顺序
    ]}]
    logger.info("[roll_initiative] 先攻顺序=%s", 先攻顺序)
    return {
        "combatants": combatants,
        "先攻顺序": 先攻顺序,
        "当前指针": -1,
        "当前轮次": 1,
        "阶段": 战斗阶段.掷先攻,
        "战斗日志": _记日志(state, 事件),
    }


# ---------------------------------------------------------------------------
# 4. next_turn（引擎）
# ---------------------------------------------------------------------------
def next_turn(state: CombatState) -> dict:
    """推进先攻指针，处理回合开始结算与跳过。

    指针在本节点入口推进（保证图里只有一条回边）。回合开始：结算持续伤害、
    递减状态；若行动者倒下 / 被突袭(首轮) / 眩晕，则跳过并继续推进。
    """
    combatants = state["combatants"]
    顺序 = state["先攻顺序"]
    指针 = state["当前指针"]
    轮次 = state["当前轮次"]
    事件: list[dict] = []

    安全计数 = 0
    while True:
        安全计数 += 1
        if 安全计数 > len(顺序) * 4 + 8:
            # 兜底：理论上不会触发（check_end 已保证两阵营都还有活人）
            break

        指针 += 1
        if 指针 >= len(顺序):
            指针 = 0
            轮次 += 1

        行动者 = combatants[顺序[指针]]

        if not 行动者.存活:
            continue  # 倒下者直接跳过，不结算

        # —— 回合开始结算：持续伤害 ——
        for s in list(行动者.状态):
            if s.状态 == 状态类型.持续伤害 and s.数值 > 0:
                实扣 = 行动者.受伤(s.数值)
                事件.append(_带轮次(state | {"当前轮次": 轮次}, {
                    "事件": "持续伤害", "行动者": 行动者.id,
                    "伤害": 实扣, "当前HP": 行动者.当前HP,
                }))

        被眩晕 = 行动者.拥有状态(状态类型.眩晕)
        行动者.递减状态()

        if not 行动者.存活:
            事件.append({"事件": "倒下", "行动者": 行动者.id, "原因": "持续伤害", "轮次": 轮次})
            continue
        if 行动者.被突袭 and 轮次 == 1:
            事件.append({"事件": "跳过", "行动者": 行动者.id, "原因": "被突袭", "轮次": 轮次})
            continue
        if 被眩晕:
            事件.append({"事件": "跳过", "行动者": 行动者.id, "原因": "眩晕", "轮次": 轮次})
            continue
        break

    logger.info("[next_turn] 轮次=%d 指针=%d 行动者=%s", 轮次, 指针, 顺序[指针])
    return {
        "combatants": combatants,
        "当前指针": 指针,
        "当前轮次": 轮次,
        "阶段": 战斗阶段.回合中,
        "当前行动": None,
        "本回合事件": [],
        "战斗日志": _记日志(state, 事件),
    }


# ---------------------------------------------------------------------------
# 5. declare_action（玩家中断 / DM）
# ---------------------------------------------------------------------------
def declare_action(state: CombatState) -> dict:
    """声明行动与目标：玩家中断选择；怪物/NPC 由 DM 决策（v0 启发式）。"""
    combatants = state["combatants"]
    行动者 = _当前行动者(state)

    if 行动者.是否玩家控制:
        选项 = 构造声明选项(行动者, combatants)
        恢复值 = interrupt(构造中断请求(
            类型=中断类型.声明行动,
            对象=行动者,
            提示=f"轮到 {行动者.名字}，声明你的行动",
            选项=选项,
        ))
        当前行动 = _规范化行动(恢复值, 行动者, combatants)
    else:
        当前行动 = _dm决策(行动者, combatants)

    logger.info("[declare_action] %s -> %s", 行动者.id, 当前行动)
    return {"当前行动": 当前行动}


def _规范化行动(恢复值: Any, 行动者: 参战者, combatants: dict[str, 参战者]) -> dict:
    """把玩家回报的恢复值规范成统一的「当前行动」结构。"""
    if not isinstance(恢复值, dict):
        return {"行动类型": 行动类型.放弃.value}
    行动 = dict(恢复值)
    行动.setdefault("行动类型", 行动类型.放弃.value)
    return 行动


def _dm决策(行动者: 参战者, combatants: dict[str, 参战者]) -> dict:
    """怪物/NPC 的确定性决策（占位，可替换为 LLM）。

    策略：选第一件能够得着存活敌人的武器，打血量最低的目标；
    都够不着 → 移动到最近敌人的区域；没有敌人 → 放弃。
    """
    敌方存活 = [c for c in combatants.values() if c.阵营 != 行动者.阵营 and c.存活]
    if not 敌方存活:
        return {"行动类型": 行动类型.放弃.value}

    for 武器 in 行动者.攻击:
        可打 = [t for t in 敌方存活 if 够得着(行动者, t, 武器.是远程)]
        if 可打:
            目标 = min(可打, key=lambda t: t.当前HP)
            return {
                "行动类型": 行动类型.攻击.value,
                "攻击名": 武器.名字,
                "目标id": 目标.id,
            }

    # 够不着任何人：移动到最近敌人的区域（下回合再打）
    目标 = min(敌方存活, key=lambda t: t.当前HP)
    return {"行动类型": 行动类型.移动.value, "目标区域": 目标.当前区域}


# ---------------------------------------------------------------------------
# 6. resolve_action（引擎 + 玩家中断）
# ---------------------------------------------------------------------------
def resolve_action(state: CombatState) -> dict:
    """按「当前行动」类型做确定性结算，产出结构化事件。"""
    combatants = state["combatants"]
    行动者 = _当前行动者(state)
    行动 = state.get("当前行动") or {"行动类型": 行动类型.放弃.value}
    类型 = 行动.get("行动类型")

    if 类型 == 行动类型.攻击.value:
        事件 = _结算攻击(行动者, 行动, combatants)
    elif 类型 == 行动类型.技能.value:
        事件 = _结算技能(行动者, 行动, combatants)
    elif 类型 == 行动类型.道具.value:
        事件 = _结算道具(行动者, 行动, combatants)
    elif 类型 == 行动类型.创意.value:
        事件 = _结算创意(行动者, 行动, combatants)
    elif 类型 == 行动类型.移动.value:
        事件 = _结算移动(行动者, 行动)
    else:
        事件 = [{"事件": "放弃", "行动者": 行动者.id}]

    事件 = [_带轮次(state, e) for e in 事件]
    logger.info("[resolve_action] %s 事件=%s", 行动者.id, [e.get("事件") for e in 事件])
    return {
        "combatants": combatants,
        "本回合事件": 事件,
        "战斗日志": _记日志(state, 事件),
    }


def _结算攻击(行动者: 参战者, 行动: dict, combatants: dict[str, 参战者]) -> list[dict]:
    """攻击结算：掷命中 → 判定 → 掷伤害 → 扣 HP，必要时置倒下。"""
    武器 = next((a for a in 行动者.攻击 if a.名字 == 行动.get("攻击名")), None)
    if 武器 is None and 行动者.攻击:
        武器 = 行动者.攻击[0]
    目标 = combatants.get(行动.get("目标id", ""))

    if 武器 is None or 目标 is None or not 目标.存活:
        return [{"事件": "无效攻击", "行动者": 行动者.id, "目标": 行动.get("目标id")}]
    if not 够得着(行动者, 目标, 武器.是远程):
        return [{"事件": "够不着", "行动者": 行动者.id, "目标": 目标.id, "攻击名": 武器.名字}]

    # —— 命中骰：玩家中断（可一并报伤害）；怪物引擎掷 ——
    玩家伤害: int | None = None
    if 行动者.是否玩家控制:
        恢复值 = interrupt(构造中断请求(
            类型=中断类型.攻击检定,
            对象=行动者,
            提示=f"{行动者.名字} 用「{武器.名字}」攻击 {目标.名字}：掷 d20 + {武器.命中加值}",
            需要骰子="d20",
            加值=武器.命中加值,
            附带={"伤害骰": 武器.伤害骰},
        ))
        d20 = 校验d20(恢复值)
        玩家伤害 = 取伤害结果(恢复值)
    else:
        d20 = _骰子.d20()

    判定 = 判定攻击(d20, 武器.命中加值, 目标.AC)
    事件: dict = {
        "事件": "攻击", "行动者": 行动者.id, "目标": 目标.id,
        "攻击名": 武器.名字, "d20": d20, "命中": 判定.命中, "重击": 判定.重击,
    }

    if not 判定.命中:
        return [事件]

    # —— 伤害骰 ——
    if 判定.重击:
        # 重击需翻倍骰数：玩家补一次伤害掷骰中断；怪物引擎翻倍掷
        if 行动者.是否玩家控制:
            恢复值 = interrupt(构造中断请求(
                类型=中断类型.伤害掷骰,
                对象=行动者,
                提示=f"重击！把 {武器.伤害骰} 的骰子数翻倍掷，报伤害总和",
                需要骰子=武器.伤害骰,
            ))
            伤害 = 取伤害结果(恢复值) or _骰子.掷(武器.伤害骰, 重击=True).总和
        else:
            伤害 = _骰子.掷(武器.伤害骰, 重击=True).总和
    else:
        if 行动者.是否玩家控制:
            伤害 = 玩家伤害 if 玩家伤害 is not None else _骰子.掷(武器.伤害骰).总和
        else:
            伤害 = _骰子.掷(武器.伤害骰).总和

    实扣 = 目标.受伤(伤害)
    事件.update({
        "伤害": 实扣, "伤害类型": str(武器.伤害类型.value),
        "目标HP": 目标.当前HP, "目标存活": 目标.存活,
    })
    return [事件]


_治疗技能 = {"skill_second_wind": "1d10"}


def _结算技能(行动者: 参战者, 行动: dict, combatants: dict[str, 参战者]) -> list[dict]:
    """技能结算（v0）：扣充能；已知治疗技能回血，其余仅记事交 DM 叙述。"""
    技能id = 行动.get("技能id", "")
    持有 = None
    if isinstance(行动者, 角色):
        持有 = next((s for s in 行动者.已学技能 if s.技能id == 技能id), None)
    if 持有 is None or not 持有.可用:
        return [{"事件": "无效技能", "行动者": 行动者.id, "技能id": 技能id}]

    持有.当前充能 -= 1
    事件: dict = {"事件": "技能", "行动者": 行动者.id, "技能id": 技能id}

    if 技能id in _治疗技能:
        治疗量 = _骰子.掷(_治疗技能[技能id]).总和 + getattr(行动者, "等级", 1)
        恢复 = 行动者.治疗(治疗量)
        事件.update({"治疗": 恢复, "目标": 行动者.id, "目标HP": 行动者.当前HP})
    return [事件]


_治疗道具 = {"item_healing_potion": "2d4+2"}


def _结算道具(行动者: 参战者, 行动: dict, combatants: dict[str, 参战者]) -> list[dict]:
    """道具结算（v0）：扣数量；已知治疗药水回血，其余仅记事。"""
    道具id = 行动.get("道具id", "")
    持有 = None
    if isinstance(行动者, 角色):
        持有 = next((i for i in 行动者.背包 if i.道具id == 道具id), None)
    if 持有 is None or not 持有.可用:
        return [{"事件": "无效道具", "行动者": 行动者.id, "道具id": 道具id}]

    持有.数量 -= 1
    目标 = combatants.get(行动.get("目标id", ""), 行动者)
    事件: dict = {"事件": "道具", "行动者": 行动者.id, "道具id": 道具id, "目标": 目标.id}

    if 道具id in _治疗道具:
        恢复 = 目标.治疗(_骰子.掷(_治疗道具[道具id]).总和)
        事件.update({"治疗": 恢复, "目标HP": 目标.当前HP})
    return [事件]


def _结算创意(行动者: 参战者, 行动: dict, combatants: dict[str, 参战者]) -> list[dict]:
    """创意动作（v0）：DM 给 DC（默认 12），行动者掷敏捷检定；引擎只判成败，效果交 DM 叙述。"""
    DC = int(行动.get("DC", 12))
    属性项 = 属性(行动.get("属性", 属性.敏捷))
    加值 = 技能加值(行动者, 属性项)

    if 行动者.是否玩家控制:
        恢复值 = interrupt(构造中断请求(
            类型=中断类型.属性检定,
            对象=行动者,
            提示=f"创意动作「{行动.get('描述', '')}」：掷 {属性项.value}检定 d20 + {加值}，对抗 DC {DC}",
            需要骰子="d20",
            加值=加值,
        ))
        d20 = 校验d20(恢复值)
    else:
        d20 = _骰子.d20()

    成功 = 判定检定(d20, 加值, DC)
    return [{
        "事件": "创意", "行动者": 行动者.id, "描述": 行动.get("描述", ""),
        "d20": d20, "DC": DC, "成功": 成功,
    }]


def _结算移动(行动者: 参战者, 行动: dict) -> list[dict]:
    """移动：改变所在区域（本版区域粒度，不算格子）。"""
    旧区域 = 行动者.当前区域
    行动者.当前区域 = 行动.get("目标区域", 旧区域)
    return [{
        "事件": "移动", "行动者": 行动者.id,
        "从": 旧区域, "到": 行动者.当前区域,
    }]


# ---------------------------------------------------------------------------
# 7. narrate（DM）
# ---------------------------------------------------------------------------
def narrate(state: CombatState) -> dict:
    """把本回合事件讲成故事。

    v0 用确定性模板生成叙述并通过 custom 流推给前端（复用现有 graph.invoke 的
    custom 事件通道）；日后可替换为 LLM（astream_agent_collect）。不改任何数值。
    """
    事件 = state.get("本回合事件", []) or []
    combatants = state["combatants"]
    句子 = [_叙述一句(e, combatants) for e in 事件]
    叙述 = " ".join(s for s in 句子 if s)

    writer = None
    try:
        writer = get_stream_writer()
    except Exception:  # 非图执行上下文（如单测直接调用）时无 writer
        writer = None
    if writer and 叙述:
        writer({"node": "narrate", "status": "start"})
        writer({"node": "narrate", "status": "streaming", "chunk": 叙述})
        writer({"node": "narrate", "status": "end"})

    日志 = _记日志(state, [{"事件": "叙述", "文本": 叙述, "轮次": state.get("当前轮次")}])
    return {"战斗日志": 日志}


def _名字(combatants: dict[str, 参战者], cid: str | None) -> str:
    c = combatants.get(cid or "")
    return c.名字 if c else (cid or "某人")


def _叙述一句(e: dict, combatants: dict[str, 参战者]) -> str:
    名 = lambda cid: _名字(combatants, cid)  # noqa: E731
    类型 = e.get("事件")
    if 类型 == "攻击":
        if not e.get("命中"):
            return f"{名(e.get('行动者'))}的{e.get('攻击名')}落空了。"
        重 = "重击！" if e.get("重击") else ""
        死 = "，将其击倒！" if e.get("目标存活") is False else "。"
        return f"{重}{名(e.get('行动者'))}的{e.get('攻击名')}命中{名(e.get('目标'))}，造成{e.get('伤害', 0)}点伤害{死}"
    if 类型 == "技能":
        if "治疗" in e:
            return f"{名(e.get('行动者'))}施展技能，恢复了{e.get('治疗')}点生命。"
        return f"{名(e.get('行动者'))}施展了一项技能。"
    if 类型 == "道具":
        if "治疗" in e:
            return f"{名(e.get('行动者'))}使用道具，为{名(e.get('目标'))}恢复{e.get('治疗')}点生命。"
        return f"{名(e.get('行动者'))}使用了一件道具。"
    if 类型 == "创意":
        return f"{名(e.get('行动者'))}尝试{e.get('描述') or '一个临场动作'}，{'成功' if e.get('成功') else '失败'}了。"
    if 类型 == "移动":
        return f"{名(e.get('行动者'))}移动到了{e.get('到')}。"
    if 类型 == "持续伤害":
        return f"{名(e.get('行动者'))}受到持续伤害{e.get('伤害')}点。"
    if 类型 == "跳过":
        return f"{名(e.get('行动者'))}因{e.get('原因')}无法行动。"
    if 类型 == "放弃":
        return f"{名(e.get('行动者'))}选择按兵不动。"
    return ""


# ---------------------------------------------------------------------------
# 8. check_end（引擎节点）+ 路由
# ---------------------------------------------------------------------------
def check_end(state: CombatState) -> dict:
    """判胜负，改写 `战斗结果`（条件由 `route_after_check` 只读路由）。"""
    combatants = state["combatants"]
    敌方存活 = any(c.存活 for c in combatants.values() if c.阵营 == 阵营.敌人)
    玩家存活 = any(c.存活 for c in combatants.values() if c.阵营 == 阵营.玩家)

    if not 敌方存活:
        结果 = 战斗结果.玩家胜
    elif not 玩家存活:
        结果 = 战斗结果.玩家败
    else:
        结果 = 战斗结果.进行中

    logger.info("[check_end] 战斗结果=%s", 结果.value)
    return {"战斗结果": 结果}


def route_after_check(state: CombatState) -> str:
    """条件边路由：进行中→继续下一位；否则→结算。"""
    return "continue" if state["战斗结果"] == 战斗结果.进行中 else "end"


# ---------------------------------------------------------------------------
# 9. settle（引擎）
# ---------------------------------------------------------------------------
def settle(state: CombatState) -> dict:
    """结算并回到剧情：置结束阶段，发战利品，导出可写回世界库的数据。"""
    场景 = state.get("场景上下文", {}) or {}
    combatants = state["combatants"]

    写回 = {
        cid: {
            "当前HP": c.当前HP,
            "存活状态": str(c.存活状态.value),
            "状态": [s.to_dict() for s in c.状态],
            "背包": [i.to_dict() for i in getattr(c, "背包", [])],
        }
        for cid, c in combatants.items()
    }
    战利品 = 场景.get("战利品表", []) if state["战斗结果"] == 战斗结果.玩家胜 else []

    事件 = [{
        "事件": "结算", "战斗结果": str(state["战斗结果"].value),
        "战利品": 战利品,
    }]
    logger.info("[settle] 战斗结束 | 结果=%s", state["战斗结果"].value)
    return {
        "阶段": 战斗阶段.结束,
        "战斗日志": _记日志(state, 事件),
        "场景上下文": {**场景, "写回": 写回, "发放战利品": 战利品},
    }
