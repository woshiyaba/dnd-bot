"""战斗子图 ↔ DM 智能体的桥接层。

职责（把"combat 怎么用 DM"集中在一处，让 nodes.py 保持精简）：
- 从 ``CombatState`` 的模型对象构造**最小上下文**文本，喂给 DM 智能体；
- 调用 ``src.dm.agent`` 的决策/叙述接口，并把结果**校验**回引擎可用的结构；
- 在装配时把引擎骰子注入 DM 骰子工具。

依赖方向合规：本模块属于 ``src.combat``，向 ``src.dm`` 注入引擎骰子，``src.dm`` 不反向依赖。
DM 决策与叙述必须由真实 LLM 完成；失败直接抛错，不允许回落确定性占位。
"""

from __future__ import annotations

import json
import logging

from src.combat.dice import current_engine_dice
from src.combat.rules import in_reach
from src.dm.agent import dm_complete_json, dm_narrate
from src.dm.tools import set_dice_provider
from src.model.combatant import Combatant
from src.model.enums import ActionType

logger = logging.getLogger(__name__)

# 把 DM 骰子工具接到引擎当前可复现骰子上（combat → dm 注入，方向合规）
set_dice_provider(current_engine_dice)


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
            {
                "name": a.name,
                "range": str(a.attack_range.value),
                "damage": a.damage_dice,
            }
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
async def judge_surprise_llm(
    combatants: dict[str, Combatant], scene: dict
) -> list[str]:
    """让 DM 判定哪些参战者被突袭，返回被突袭者 id 列表。

    DM 可用骰子做"潜行 vs 被动察觉"对抗、可查 passive_check 规则。结果只取
    确实存在于本场的 id。``scene_context["surprise_context"]`` 可给 DM 额外背景。
    """
    roster = [_brief(c) for c in combatants.values()]
    hint = (
        scene.get("surprise_context")
        or "（无额外背景，按常理判断是否有人被打个措手不及）"
    )
    task = (
        "战斗即将开始，请判定本场是否有参战者陷入【突袭】（被突袭者将跳过自己的第一个回合）。\n"
        f"背景：{hint}\n"
        f"参战者名单：{_dump(roster)}\n"
        "如需要，可用骰子做潜行对抗、用 kb_read passive_check 查被动察觉规则。\n"
        '最终只输出 JSON：{"surprised": ["被突袭者的id", ...]}（无人被突袭则空数组）。'
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict) or "surprised" not in data:
        raise RuntimeError("[combat.dm] 突袭判定未返回合法 JSON")
    ids = data.get("surprised") or []
    if not isinstance(ids, list):
        raise ValueError("[combat.dm] surprised 必须是数组")
    return [str(cid) for cid in ids if str(cid) in combatants]


# ---------------------------------------------------------------------------
# 2. 怪物/NPC 行动决策
# ---------------------------------------------------------------------------
async def decide_action_llm(
    actor: Combatant,
    combatants: dict[str, Combatant],
    *,
    attack_only: bool = False,
) -> dict:
    """让 DM 替怪物/NPC 决定本回合动作，返回规范化的 action 字典。

    仅允许从"行动者已有的攻击 + 存活敌人"中选择；攻击需目标存活且够得着，
    否则视为无效并抛错。
    """
    enemies = [
        c for c in combatants.values() if c.faction != actor.faction and c.is_alive
    ]
    if not enemies:
        return {"action_type": ActionType.PASS.value}

    reachable = {
        a.name: [t.id for t in enemies if in_reach(actor, t, a.is_ranged)]
        for a in actor.attacks
    }
    output_choices = (
        "最终只输出 JSON，二选一：\n"
        '{"action_type":"attack","attack_name":"...","target_id":"..."}\n'
        '{"action_type":"pass"}'
        if attack_only
        else (
            "最终只输出 JSON，三选一：\n"
            '{"action_type":"attack","attack_name":"...","target_id":"..."}\n'
            '{"action_type":"move","target_zone":"..."}\n'
            '{"action_type":"pass"}'
        )
    )
    task = (
        f"轮到你操控的「{actor.name}」行动，请为它选择本回合的动作。\n"
        f"行动者：{_dump(_brief(actor))}\n"
        f"存活敌人：{_dump([_brief(e) for e in enemies])}\n"
        f"各攻击当前可直接命中的敌人 id：{_dump(reachable)}\n"
        "规则：只能用上面列出的攻击；攻击目标必须在该攻击的可命中列表里；"
        + (
            "当前只剩额外攻击，不能移动，只能攻击或放弃。\n"
            if attack_only
            else "够不着任何人就移动到某个敌人的区域；没有敌人就放弃。\n"
        )
        + "可 kb_read 查这个怪物的打法倾向来决定目标与风格。\n"
        + output_choices
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict):
        raise RuntimeError("[combat.dm] 行动决策未返回合法 JSON")

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
        raise ValueError("[combat.dm] DM 返回了非法攻击目标")
    if (
        not attack_only
        and action_type == ActionType.MOVE.value
        and data.get("target_zone")
    ):
        return {
            "action_type": ActionType.MOVE.value,
            "target_zone": str(data["target_zone"]),
        }
    if action_type == ActionType.PASS.value:
        return {"action_type": ActionType.PASS.value}
    raise ValueError(f"[combat.dm] 未知行动类型：{action_type!r}")


# ---------------------------------------------------------------------------
# 3. 玩家自然语言行动裁定
# ---------------------------------------------------------------------------
async def adjudicate_player_action_llm(
    actor: Combatant,
    text: str,
    options: dict,
    combatants: dict[str, Combatant],
    scene: dict,
) -> dict:
    """把玩家自然语言映射为当前合法行动；无法映射时明确拒绝且不消耗回合。

    LLM 只能从引擎提供的封闭选项中挑选，返回后还会再次做确定性校验。它不能
    自创 DC、伤害、状态或目标，也不能直接结算任何规则效果。
    """
    task = (
        f"当前轮到「{actor.name}」行动。玩家说：{text!r}\n"
        f"行动者：{_dump(_brief(actor))}\n"
        f"当前参战者：{_dump([_brief(c) for c in combatants.values()])}\n"
        f"引擎给出的全部合法选项：{_dump(options)}\n"
        f"场景提示：{_dump({'location': scene.get('location'), 'reason': scene.get('reason')})}\n"
        "你的职责只是解释玩家意图并选择一个合法选项，不能创造新规则、DC、效果、"
        "攻击、目标或移动区域。表达能清楚对应攻击、移动、规则行动或"
        "放弃时，输出 accepted=true 以及对应 action；否则 accepted=false，并用简短"
        "中文说明当前可行的改法。不要结算命中、伤害或 HP。\n"
        "只输出 JSON：\n"
        '{"accepted":true,"action":{"action_type":"attack|move|rule_action|pass",'
        '"attack_name":"可选","target_id":"可选","target_zone":"可选",'
        '"action_id":"规则行动时填写","target_ids":["可选目标"]}}\n'
        '或 {"accepted":false,"reason":"简短中文反馈"}'
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict) or not isinstance(data.get("accepted"), bool):
        raise RuntimeError("[combat.dm] 玩家行动裁定未返回合法 JSON")
    if not data["accepted"]:
        reason = str(data.get("reason", "")).strip()
        return {
            "accepted": False,
            "reason": reason or "这项行动目前无法对应到合法选项，请换一种做法。",
        }

    normalized = validate_player_action(data.get("action"), options, combatants)
    if normalized is None:
        return {
            "accepted": False,
            "reason": "这项行动目前无法对应到合法选项，请从当前可用行动中重新描述。",
        }
    return {"accepted": True, "action": normalized}


def validate_player_action(
    raw_action: object,
    options: dict,
    combatants: dict[str, Combatant],
) -> dict | None:
    """确定性验证 LLM 选中的行动确实属于当前选项集合。"""
    if not isinstance(raw_action, dict):
        return None
    action_type = raw_action.get("action_type")
    if action_type == ActionType.ATTACK.value:
        attack_name = raw_action.get("attack_name")
        target_id = raw_action.get("target_id")
        for attack in options.get("attack", []) or []:
            target_ids = {target.get("id") for target in attack.get("targets", [])}
            if attack.get("attack_name") == attack_name and target_id in target_ids:
                return {
                    "action_type": ActionType.ATTACK.value,
                    "attack_name": str(attack_name),
                    "target_id": str(target_id),
                }
        return None
    if action_type == ActionType.MOVE.value:
        target_zone = raw_action.get("target_zone")
        legal_zones = {
            move.get("target_zone") for move in options.get("move", []) or []
        }
        if target_zone in legal_zones:
            return {
                "action_type": ActionType.MOVE.value,
                "target_zone": str(target_zone),
            }
        return None
    if action_type == ActionType.RULE_ACTION.value:
        entries = options.get("rule_actions", []) or []
        selected_id = raw_action.get("action_id")
        selected_entry = next(
            (
                entry
                for entry in entries
                if entry.get("action_id") == selected_id and entry.get("enabled")
            ),
            None,
        )
        if selected_entry is None:
            return None
        normalized = {
            "action_type": ActionType.RULE_ACTION.value,
            "action_id": str(selected_id),
        }
        target_ids = list(raw_action.get("target_ids") or [])
        if raw_action.get("target_id") is not None and not target_ids:
            target_ids = [raw_action["target_id"]]
        normalized_targets = list(dict.fromkeys(map(str, target_ids)))
        if not (
            int(selected_entry.get("min_targets", 0))
            <= len(normalized_targets)
            <= int(selected_entry.get("max_targets", 20))
        ):
            return None
        legal_targets = {
            str(target.get("id"))
            for target in selected_entry.get("targets", [])
            if target.get("id")
        }
        if not set(normalized_targets).issubset(legal_targets):
            return None
        normalized["target_ids"] = normalized_targets
        if normalized_targets:
            normalized["target_id"] = normalized_targets[0]
        return normalized
    if action_type == ActionType.PASS.value and options.get("pass"):
        return {"action_type": ActionType.PASS.value}
    return None


# ---------------------------------------------------------------------------
# 4. 叙述
# ---------------------------------------------------------------------------
async def narrate_combat_opening_llm(
    combatants: dict[str, Combatant],
    scene: dict,
) -> str:
    """根据已经提交的参战名单叙述开战瞬间，不提前结算任何攻击。"""
    task = (
        "战斗已经由 DM 裁定成立，下面的参战者与场景是引擎已经提交的事实：\n"
        f"场景：{_dump({'location': scene.get('location'), 'reason': scene.get('reason')})}\n"
        f"参战者：{_dump([_brief(c) for c in combatants.values()])}\n"
        "请用 2-4 句中文叙述冲突如何正式进入战斗，并让读者清楚谁在对峙。"
        "这只是开场镜头：不要描述任何攻击已经命中、伤害、HP 变化、死亡或胜负。"
    )
    return await dm_narrate(task)


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
