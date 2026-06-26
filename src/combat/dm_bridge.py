"""战斗子图 ↔ DM 智能体的桥接层。

职责（把"combat 怎么用 DM"集中在一处，让 nodes.py 保持精简）：
- 从 ``CombatState`` 的模型对象构造**最小上下文**文本，喂给 DM 智能体；
- 调用 ``src.dm.agent`` 的决策/叙述接口，并把结果**校验**回引擎可用的结构；
- 提供 ``dm_mode`` 开关判断，并在装配时把引擎骰子注入 DM 骰子工具。

依赖方向合规：本模块属于 ``src.combat``，向 ``src.dm`` 注入引擎骰子，``src.dm`` 不反向依赖。
失败一律返回 None / 抛错由调用方捕获，使战斗能回落到确定性占位逻辑。
"""

from __future__ import annotations

import json
import logging
import os

from src.combat.dice import current_engine_dice
from src.combat.rules import in_reach
from src.dm.agent import dm_complete_json, dm_narrate
from src.dm.tools import set_dice_provider
from src.model.combatant import Combatant
from src.model.enums import ActionType

logger = logging.getLogger(__name__)

# 把 DM 骰子工具接到引擎当前可复现骰子上（combat → dm 注入，方向合规）
set_dice_provider(current_engine_dice)


def dm_enabled(scene: dict | None) -> bool:
    """是否启用 LLM 版 DM：需 ``scene_context["dm_mode"] == "llm"`` 且配置了 API Key。

    缺 Key 时记一条 warning 并返回 False，让战斗回落到确定性占位（仍可独立运行）。
    """
    if (scene or {}).get("dm_mode") != "llm":
        return False
    if not os.getenv("DASHSCOPE_API_KEY"):
        logger.warning("[dm] dm_mode=llm 但未配置 DASHSCOPE_API_KEY，回落启发式 DM")
        return False
    return True


# ---------------------------------------------------------------------------
# 上下文构造
# ---------------------------------------------------------------------------
def _brief(c: Combatant) -> dict:
    """把参战者压缩成喂给 DM 的最小画像（不下发整个对象）。"""
    return {
        "id": c.id,
        "name": c.name,
        "faction": str(c.faction.value),
        "hp": f"{c.current_hp}/{c.max_hp}",
        "ac": c.ac,
        "zone": c.current_zone,
        "alive": c.is_alive,
        "attacks": [
            {"name": a.name, "range": str(a.attack_range.value), "damage": a.damage_dice}
            for a in c.attacks
        ],
        "conditions": [str(s.kind.value) for s in c.conditions],
    }


def _dump(obj) -> str:
    """紧凑 JSON 序列化（中文不转义），用于拼进任务文本。"""
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1. 判突袭
# ---------------------------------------------------------------------------
async def judge_surprise_llm(combatants: dict[str, Combatant], scene: dict) -> list[str] | None:
    """让 DM 判定哪些参战者被突袭，返回被突袭者 id 列表；无法判定返回 None。

    DM 可用骰子做"潜行 vs 被动察觉"对抗、可查 passive_check 规则。结果只取
    确实存在于本场的 id。``scene_context["surprise_context"]`` 可给 DM 额外背景。
    """
    roster = [_brief(c) for c in combatants.values()]
    hint = scene.get("surprise_context") or "（无额外背景，按常理判断是否有人被打个措手不及）"
    task = (
        "战斗即将开始，请判定本场是否有参战者陷入【突袭】（被突袭者将跳过自己的第一个回合）。\n"
        f"背景：{hint}\n"
        f"参战者名单：{_dump(roster)}\n"
        "如需要，可用骰子做潜行对抗、用 kb_read passive_check 查被动察觉规则。\n"
        '最终只输出 JSON：{"surprised": ["被突袭者的id", ...]}（无人被突袭则空数组）。'
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict) or "surprised" not in data:
        return None
    ids = data.get("surprised") or []
    if not isinstance(ids, list):
        return None
    return [str(cid) for cid in ids if str(cid) in combatants]


# ---------------------------------------------------------------------------
# 2. 怪物/NPC 行动决策
# ---------------------------------------------------------------------------
async def decide_action_llm(actor: Combatant, combatants: dict[str, Combatant]) -> dict | None:
    """让 DM 替怪物/NPC 决定本回合动作，返回规范化的 action 字典；无法采纳返回 None。

    仅允许从"行动者已有的攻击 + 存活敌人"中选择；攻击需目标存活且够得着，
    否则视为无效、返回 None 让引擎回落到启发式。
    """
    enemies = [c for c in combatants.values() if c.faction != actor.faction and c.is_alive]
    if not enemies:
        return {"action_type": ActionType.PASS.value}

    reachable = {
        a.name: [t.id for t in enemies if in_reach(actor, t, a.is_ranged)]
        for a in actor.attacks
    }
    task = (
        f"轮到你操控的「{actor.name}」行动，请为它选择本回合的动作。\n"
        f"行动者：{_dump(_brief(actor))}\n"
        f"存活敌人：{_dump([_brief(e) for e in enemies])}\n"
        f"各攻击当前可直接命中的敌人 id：{_dump(reachable)}\n"
        "规则：只能用上面列出的攻击；攻击目标必须在该攻击的可命中列表里；"
        "够不着任何人就移动到某个敌人的区域；没有敌人就放弃。\n"
        "可 kb_read 查这个怪物的打法倾向来决定目标与风格。\n"
        '最终只输出 JSON，三选一：\n'
        '{"action_type":"attack","attack_name":"...","target_id":"..."}\n'
        '{"action_type":"move","target_zone":"..."}\n'
        '{"action_type":"pass"}'
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict):
        return None

    action_type = data.get("action_type")
    if action_type == ActionType.ATTACK.value:
        attack_name = data.get("attack_name")
        target_id = data.get("target_id")
        if attack_name in reachable and target_id in reachable.get(attack_name, []):
            return {
                "action_type": ActionType.ATTACK.value,
                "attack_name": attack_name,
                "target_id": target_id,
            }
        return None  # 非法攻击 → 回落启发式
    if action_type == ActionType.MOVE.value and data.get("target_zone"):
        return {"action_type": ActionType.MOVE.value, "target_zone": str(data["target_zone"])}
    if action_type == ActionType.PASS.value:
        return {"action_type": ActionType.PASS.value}
    return None


# ---------------------------------------------------------------------------
# 3. 叙述
# ---------------------------------------------------------------------------
async def narrate_llm(
    events: list[dict],
    combatants: dict[str, Combatant],
    round_no: int | None,
) -> str:
    """让 DM 把本回合结构化事件讲成中文叙述（流式推前端），返回完整叙述文本。

    把事件里的 id 先换成名字再喂给 DM，并强调"只描述已发生的事实、不新增数值"。
    """
    id_to_name = {cid: c.name for cid, c in combatants.items()}

    def _label(value):
        return id_to_name.get(value, value)

    readable = []
    for e in events:
        item = dict(e)
        for key in ("actor", "target"):
            if key in item:
                item[key] = _label(item[key])
        readable.append(item)

    task = (
        f"这是战斗第 {round_no} 轮刚刚结算出的事件（已由引擎判定，数字是既定事实）：\n"
        f"{_dump(readable)}\n"
        "请把它讲成一段简洁、有画面感的中文叙述（2-4 句）。只描述这些已发生的事实，"
        "不要新增伤害数字、命中结果或谁的死活，也不要罗列字段。"
    )
    return await dm_narrate(task)
