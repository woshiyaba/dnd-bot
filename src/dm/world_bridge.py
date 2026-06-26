"""中央 DM 的「世界桥接」：把世界状态喂给 DM 智能体，并取回决策 / 叙述。

职责（**纯 DM 层**，依赖方向合规：本模块只依赖 ``src.dm`` 与 ``src.model``，不碰
``src.combat``——检定结算、中断构造、战斗输入装配等需要规则引擎的活，交给上层
``src.session`` 处理）：

- :func:`decide_turn` —— DM 读「场景 + 对话 + 玩家输入」，决定本回合意图：
  ``reply``（直接叙述，可自掷暗骰）/ ``player_check``（要玩家明骰）/ ``start_combat``（开战）。
  LLM 模式走 :func:`dm_complete_json`；无模型时回落关键词启发式，保证可离线跑。
- :func:`narrate_reply` / :func:`narrate_result` / :func:`narrate_aftermath` ——
  把要对玩家说的话流式推前端（复用 custom 通道）。

决策只产出「规格」（如检定的 ability/dc/kind、遭遇的 monster_ids），**不计算加值、不判成败、
不组装战斗参战者**——那是引擎的事，放在 ``src.session``，以守住「规则归引擎」。
"""

from __future__ import annotations

import json
import logging

from src.common.utils.writer import StreamCollector
from src.dm.agent import dm_complete_json, dm_narrate
from src.model.dm_state import hostile_actors
from src.model.enums import Ability
from src.model.combatant import Combatant

logger = logging.getLogger(__name__)

# 合法属性值（校验 DM 给的 ability）
_ABILITY_VALUES = {a.value for a in Ability}
_CHECK_KINDS = {"ability_check", "saving_throw"}
_INTENTS = {"reply", "player_check", "start_combat"}


# ---------------------------------------------------------------------------
# 上下文构造（喂给 DM 的最小画像，控延迟）
# ---------------------------------------------------------------------------
def _dump(obj) -> str:
    """紧凑 JSON（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)


def _party_brief(party: dict[str, Combatant]) -> list[dict]:
    """玩家角色册压成最小画像。"""
    return [
        {"id": c.id, "name": c.name, "hp": f"{c.current_hp}/{c.max_hp}",
         "class": getattr(c, "char_class", None), "alive": c.is_alive}
        for c in party.values()
    ]


def _scene_brief(scene: dict) -> dict:
    """世界场景压成最小画像（地点 / 描述 / 在场者 / 出口 / 威胁）。"""
    actors = [
        {"actor_id": a.get("actor_id"), "name": a.get("name"),
         "disposition": a.get("disposition")}
        for a in (scene or {}).get("actors", [])
    ]
    return {
        "location": (scene or {}).get("location"),
        "description": (scene or {}).get("description"),
        "actors": actors,
        "exits": (scene or {}).get("exits", []),
        "threat": (scene or {}).get("threat"),
    }


def _history_brief(messages: list[dict], limit: int = 6) -> list[dict]:
    """取最近 limit 条对话（截断，控延迟）。"""
    return [{"role": m.get("role"), "content": m.get("content")} for m in (messages or [])[-limit:]]


# ---------------------------------------------------------------------------
# 决策：DM 读局面，给出本回合意图
# ---------------------------------------------------------------------------
async def decide_turn(
    user_input: str,
    scene: dict,
    party: dict[str, Combatant],
    *,
    messages: list[dict] | None = None,
    use_llm: bool | None = True,
) -> dict:
    """让 DM 决定本回合意图，返回规范化决策字典。

    返回形如::

        {"intent": "reply", "say": "……"}
        {"intent": "player_check",
         "check": {"actor_id","ability","dc","kind","proficient","prompt","reason"}}
        {"intent": "start_combat",
         "encounter": {"monster_ids": [...], "surprised": [...], "reason": "..."}}

    use_llm=False 时走关键词启发式（可离线、确定性），保证 DASHSCOPE 不可用也能跑通。
    """
    party_ids = list(party.keys())
    if use_llm:
        data = await _decide_llm(user_input, scene, party, messages or [])
        if data is not None:
            return _normalize_decision(data, scene, party_ids)
        logger.warning("[dm] decide_turn LLM 解析失败，回落启发式")
    return _decide_heuristic(user_input, scene, party_ids)


async def _decide_llm(user_input, scene, party, messages) -> dict | None:
    """LLM 决策：拼最小上下文 + 严格 JSON 格式要求，调 DM 智能体。"""
    task = (
        "你在主持一场 D&D 冒险。请阅读当前局面，决定如何回应玩家这一步，并**只输出一个 JSON 对象**。\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"玩家队伍：{_dump(_party_brief(party))}\n"
        f"最近对话：{_dump(_history_brief(messages))}\n"
        f"玩家这一步说/做：{user_input}\n\n"
        "判断本步属于以下哪一类（判据见你的系统提示）：\n"
        "1) 纯叙事/社交/信息，或该掷暗骰（陷阱、对抗、环境）——你可调骰子工具自己掷，"
        "把结果编进叙述，输出 {\"intent\":\"reply\",\"say\":\"给玩家看的叙述\"}。\n"
        "2) 玩家主动做一件结果不确定且成败都有意义的事（撬锁/说服/跳跃/豁免…）——交玩家明骰，"
        "输出 {\"intent\":\"player_check\",\"check\":{\"actor_id\":\"哪个玩家角色id\",\"ability\":\"strength|dexterity|constitution|intelligence|wisdom|charisma\","
        "\"dc\":数字,\"kind\":\"ability_check|saving_throw\",\"proficient\":true/false,\"prompt\":\"提示玩家掷什么\",\"reason\":\"为什么要检定\"}}。\n"
        "3) 局势升级为战斗——输出 {\"intent\":\"start_combat\",\"encounter\":{\"monster_ids\":[\"场景里敌意在场者的actor_id\"],\"surprised\":[\"被突袭者id\"],\"reason\":\"...\"}}。\n"
        "不确定 DC 时可 kb_read ability_check / 即兴伤害表。只输出 JSON，不要额外文字。"
    )
    return await dm_complete_json(task)


def _normalize_decision(data: dict, scene: dict, party_ids: list[str]) -> dict:
    """校验并规范化 DM 给的决策；非法字段一律回落到安全值。"""
    intent = data.get("intent")
    if intent not in _INTENTS:
        return {"intent": "reply", "say": str(data.get("say") or "（你环顾四周，等待着什么。）")}

    if intent == "reply":
        return {"intent": "reply", "say": str(data.get("say") or "")}

    if intent == "player_check":
        check = data.get("check") or {}
        ability = check.get("ability")
        if ability not in _ABILITY_VALUES:
            ability = Ability.DEXTERITY.value
        actor_id = check.get("actor_id")
        if actor_id not in party_ids:
            actor_id = party_ids[0] if party_ids else None
        if actor_id is None:  # 无玩家角色可检定 → 回落叙述
            return {"intent": "reply", "say": str(check.get("reason") or "")}
        kind = check.get("kind") if check.get("kind") in _CHECK_KINDS else "ability_check"
        return {"intent": "player_check", "check": {
            "actor_id": actor_id,
            "ability": ability,
            "dc": _safe_int(check.get("dc"), 12),
            "kind": kind,
            "proficient": bool(check.get("proficient", False)),
            "prompt": str(check.get("prompt") or "请掷 d20"),
            "reason": str(check.get("reason") or ""),
        }}

    # start_combat：把 monster_ids 收敛到场景里真实存在的敌意在场者
    encounter = data.get("encounter") or {}
    hostiles = {a["actor_id"]: a for a in hostile_actors(scene) if a.get("actor_id")}
    chosen = [mid for mid in (encounter.get("monster_ids") or []) if mid in hostiles]
    if not chosen:
        chosen = list(hostiles.keys())  # DM 没给或给错 → 全部敌意在场者参战
    if not chosen:  # 场景里压根没有敌人 → 无法开战，回落叙述
        return {"intent": "reply", "say": str(encounter.get("reason") or "这里并没有敌人。")}
    surprised = [sid for sid in (encounter.get("surprised") or []) if sid in chosen]
    return {"intent": "start_combat", "encounter": {
        "monster_ids": chosen,
        "surprised": surprised,
        "reason": str(encounter.get("reason") or ""),
    }}


def _safe_int(value, default: int) -> int:
    """容错取整。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 启发式决策（离线/无模型回落，确定性）
# ---------------------------------------------------------------------------
# 攻击/开战关键词
_COMBAT_WORDS = ("攻击", "开打", "动手", "拔剑", "砍", "杀", "战斗", "冲上去", "打它", "打他", "宣战", "attack")

# 检定关键词 → (属性, DC, kind)；命中即让玩家明骰
_CHECK_WORDS: list[tuple[tuple[str, ...], str, int, str]] = [
    (("撬锁", "开锁", "撬开"), Ability.DEXTERITY.value, 15, "ability_check"),
    (("说服", "劝说", "谈判", "游说"), Ability.CHARISMA.value, 13, "ability_check"),
    (("欺骗", "唬", "蒙骗"), Ability.CHARISMA.value, 13, "ability_check"),
    (("威吓", "恐吓"), Ability.CHARISMA.value, 13, "ability_check"),
    (("搜索", "搜查", "调查", "查看", "检查", "翻找"), Ability.INTELLIGENCE.value, 12, "ability_check"),
    (("察觉", "感知", "留意", "倾听", "察看"), Ability.WISDOM.value, 12, "ability_check"),
    (("跳", "攀爬", "攀登", "推开", "搬", "掰", "破门"), Ability.STRENGTH.value, 13, "ability_check"),
    (("潜行", "躲", "藏", "溜"), Ability.DEXTERITY.value, 13, "ability_check"),
    (("豁免", "抵抗", "闪避"), Ability.DEXTERITY.value, 13, "saving_throw"),
]


def _decide_heuristic(user_input: str, scene: dict, party_ids: list[str]) -> dict:
    """关键词启发式：先看是否开战，再看是否检定，否则纯叙述。"""
    text = user_input or ""
    hostiles = hostile_actors(scene)

    # 1) 开战：有攻击意图且场上有敌意在场者
    if hostiles and any(w in text for w in _COMBAT_WORDS):
        return {"intent": "start_combat", "encounter": {
            "monster_ids": [a["actor_id"] for a in hostiles if a.get("actor_id")],
            "surprised": [sid for sid in (scene or {}).get("surprised", [])],
            "reason": "玩家发起攻击",
        }}

    # 2) 检定：命中关键词且有玩家角色可掷
    if party_ids:
        for keywords, ability, dc, kind in _CHECK_WORDS:
            if any(k in text for k in keywords):
                return {"intent": "player_check", "check": {
                    "actor_id": party_ids[0],
                    "ability": ability,
                    "dc": dc,
                    "kind": kind,
                    "proficient": False,
                    "prompt": f"请掷 d20（{ability} 检定，DC {dc}）",
                    "reason": f"你尝试「{text}」，结果尚不确定。",
                }}

    # 3) 纯叙述：模板回应（离线占位，接 LLM 后被替换）
    loc = (scene or {}).get("location") or "此地"
    return {"intent": "reply", "say": f"（{loc}）你说：「{text}」。四下安静，故事在等你的下一步。"}


# ---------------------------------------------------------------------------
# 叙述：把要对玩家说的话推给前端
# ---------------------------------------------------------------------------
def narrate_reply(text: str, *, node_name: str = "dm") -> str:
    """把 DM 的一段话推给前端（custom 通道，整段一次推）。返回原文。

    用于 ``intent=reply``：决策时 DM 已生成好这段话，这里只负责推流，避免再调一次模型。
    """
    collector = StreamCollector(node_name)
    collector.start()
    try:
        if text:
            collector.push(text)
    finally:
        collector.finish()
    return collector.result


async def narrate_result(check_result: dict, *, use_llm: bool, node_name: str = "dm") -> str:
    """叙述一次玩家检定的成败（成功→「是,然后…」，失败→「不,但是…」）。"""
    if use_llm:
        verdict = "成功" if check_result.get("success") else "失败"
        task = (
            "玩家刚完成一次检定，结果已由引擎判定（既定事实，别改数字）：\n"
            f"{_dump(check_result)}\n"
            f"判定为【{verdict}】。请用 1-3 句生动的中文叙述这个结果：成功就「是，然后…」推进，"
            "失败就「不，但是…」给条出路，让故事继续。只描述结果，别罗列字段。"
        )
        return await dm_narrate(task, node_name=node_name)
    # 离线模板
    ok = check_result.get("success")
    tail = "你做到了，事情顺势展开。" if ok else "没能成功，但门路并未完全堵死。"
    return narrate_reply(f"（检定{'成功' if ok else '失败'}：{check_result.get('total')} vs DC {check_result.get('dc')}）{tail}",
                         node_name=node_name)


async def narrate_aftermath(last_combat: dict, scene: dict, *, use_llm: bool, node_name: str = "dm") -> str:
    """战斗结束后，叙述战后世界（谁倒下、战利品、接下来），把故事交回 DM。"""
    if use_llm:
        task = (
            "一场战斗刚结束，结果已由战斗引擎结算（既定事实）：\n"
            f"{_dump(last_combat)}\n"
            f"当前场景：{_dump(_scene_brief(scene))}\n"
            "请用 2-4 句中文收尾这场战斗：胜负、伤亡、捡到的战利品，并自然地把镜头交回玩家，"
            "邀请他们决定下一步。只描述既定结果，别新增战斗数字。"
        )
        return await dm_narrate(task, node_name=node_name)
    outcome = (last_combat or {}).get("outcome")
    loot = (last_combat or {}).get("granted_loot")
    won = outcome == "players_win"
    msg = ("尘埃落定，你们赢了。" if won else "战斗失利……") + (f" 战利品：{loot}。" if loot else "") + " 接下来你打算怎么做？"
    return narrate_reply(msg, node_name=node_name)
