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
        {
            "id": c.id,
            "name": c.name,
            "hp": f"{c.current_hp}/{c.max_hp}",
            "class": getattr(c, "char_class", None),
            "alive": c.is_alive,
        }
        for c in party.values()
    ]


def _scene_brief(scene: dict) -> dict:
    """世界场景压成最小画像（地点 / 描述 / 在场者 / 出口 / 威胁）。"""
    actors = [
        {
            "actor_id": a.get("actor_id"),
            "name": a.get("name"),
            "disposition": a.get("disposition"),
        }
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
    return [
        {"role": m.get("role"), "content": m.get("content")}
        for m in (messages or [])[-limit:]
    ]


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
    beat_brief: dict | None = None,
    stuck_hint: str | None = None,
) -> dict:
    """让 DM 决定本回合意图，返回规范化决策字典。

    返回形如::

        {"intent": "reply", "say": "……"}
        {"intent": "player_check",
         "check": {"actor_id","ability","dc","kind","proficient","prompt","reason"}}
        {"intent": "start_combat",
         "encounter": {"monster_ids": [...], "surprised": [...], "reason": "..."}}

    任一意图都可附带可选的世界写入声明 ``flags_set`` / ``moved_to`` / ``clues_delivered``
    （白名单校验由引擎在 evaluate_advancement 做）。

    :param beat_brief: 当前剧情拍骨架（目标/未传达线索/在场 NPC 目标秘密/出口提示），让叙述长在骨架上。
    :param stuck_hint: 卡关兜底指令（空转太久时注入），提示 DM 主动抛线索或指向出口。
    use_llm=False 时走关键词启发式（可离线、确定性），保证 DASHSCOPE 不可用也能跑通。
    """
    party_ids = list(party.keys())
    if use_llm:
        data = await _decide_llm(
            user_input, scene, party, messages or [], beat_brief, stuck_hint
        )
        if data is not None:
            return _normalize_decision(data, scene, party_ids)
        logger.warning("[dm] decide_turn LLM 解析失败，回落启发式")
    return _decide_heuristic(user_input, scene, party_ids)


async def _decide_llm(
    user_input, scene, party, messages, beat_brief=None, stuck_hint=None
) -> dict | None:
    """LLM 决策：拼最小上下文（含当前拍骨架）+ 严格 JSON 格式要求，调 DM 智能体。"""
    beat_line = (
        f"【当前剧情拍·只供你把控方向，勿照搬，尤其别直接抖出 NPC 秘密】{_dump(beat_brief)}\n"
        if beat_brief
        else ""
    )
    stuck_line = f"【推进提示】{stuck_hint}\n" if stuck_hint else ""
    task = (
        "你在主持一场有预定剧本(canon)的 D&D 冒险。请阅读当前局面，决定如何回应玩家这一步，并**只输出一个 JSON 对象**。\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"玩家队伍：{_dump(_party_brief(party))}\n"
        f"最近对话：{_dump(_history_brief(messages))}\n"
        f"{beat_line}"
        f"{stuck_line}"
        f"玩家这一步说/做：{user_input}\n\n"
        "叙述要自然朝当前拍目标推进但不硬拽玩家；你无权跳拍或改写骨架，推进由引擎判定。\n"
        "判断本步属于以下哪一类（判据见你的系统提示）：\n"
        "1) 纯叙事/社交/信息，或该掷暗骰（陷阱、对抗、环境）——你可调骰子工具自己掷，"
        '把结果编进叙述，输出 {"intent":"reply","say":"给玩家看的叙述"}。\n'
        "2) 玩家主动做一件结果不确定且成败都有意义的事（撬锁/说服/跳跃/豁免…）——交玩家明骰，"
        '输出 {"intent":"player_check","check":{"actor_id":"哪个玩家角色id","ability":"strength|dexterity|constitution|intelligence|wisdom|charisma",'
        '"dc":数字,"kind":"ability_check|saving_throw","proficient":true/false,"prompt":"提示玩家掷什么","reason":"为什么要检定"}}。\n'
        '3) 局势升级为战斗——输出 {"intent":"start_combat","encounter":{"monster_ids":["场景里敌意在场者的actor_id"],"surprised":["被突袭者id"],"reason":"..."}}。\n'
        "【可选·世界写入】当玩家这步确实改变了世界时，可在 JSON 里附带（不改变上面的 intent）：\n"
        '  "flags_set":{"flag名":true} —— 仅声明 canon 白名单内的世界 flag（如玩家发现了某条线索）；\n'
        '  "moved_to":"地点id" —— 玩家移动到的当前拍内地点；\n'
        '  "clues_delivered":["你这步已讲给玩家的关键线索id"]。\n'
        "不确定 DC 时可 kb_read ability_check / 即兴伤害表。只输出 JSON，不要额外文字。"
    )
    return await dm_complete_json(task)


def _world_writes(data: dict) -> dict:
    """从 DM 决策里抽出可选的世界写入声明（类型清洗；白名单校验留给引擎）。

    返回形如 ``{"flags_set": {...}, "moved_to": "loc_id", "clues_delivered": [...]}``；
    无任何声明时返回空 dict。
    """
    writes: dict = {}
    flags_set = data.get("flags_set")
    if isinstance(flags_set, dict) and flags_set:
        writes["flags_set"] = {str(k): v for k, v in flags_set.items()}
    moved_to = data.get("moved_to")
    if isinstance(moved_to, str) and moved_to:
        writes["moved_to"] = moved_to
    clues = data.get("clues_delivered")
    if isinstance(clues, list) and clues:
        writes["clues_delivered"] = [str(c) for c in clues]
    return writes


def _normalize_decision(data: dict, scene: dict, party_ids: list[str]) -> dict:
    """校验并规范化 DM 给的决策；非法字段一律回落到安全值。

    任一意图都会带上 ``world_writes`` 字段（可能为空 dict），承载 DM 声明的世界变化。
    """
    writes = _world_writes(data)
    intent = data.get("intent")
    if intent not in _INTENTS:
        return {
            "intent": "reply",
            "say": str(data.get("say") or "（你环顾四周，等待着什么。）"),
            "world_writes": writes,
        }

    if intent == "reply":
        return {
            "intent": "reply",
            "say": str(data.get("say") or ""),
            "world_writes": writes,
        }

    if intent == "player_check":
        check = data.get("check") or {}
        ability = check.get("ability")
        if ability not in _ABILITY_VALUES:
            ability = Ability.DEXTERITY.value
        actor_id = check.get("actor_id")
        if actor_id not in party_ids:
            actor_id = party_ids[0] if party_ids else None
        if actor_id is None:  # 无玩家角色可检定 → 回落叙述
            return {
                "intent": "reply",
                "say": str(check.get("reason") or ""),
                "world_writes": writes,
            }
        kind = (
            check.get("kind") if check.get("kind") in _CHECK_KINDS else "ability_check"
        )
        return {
            "intent": "player_check",
            "world_writes": writes,
            "check": {
                "actor_id": actor_id,
                "ability": ability,
                "dc": _safe_int(check.get("dc"), 12),
                "kind": kind,
                "proficient": bool(check.get("proficient", False)),
                "prompt": str(check.get("prompt") or "请掷 d20"),
                "reason": str(check.get("reason") or ""),
            },
        }

    # start_combat：把 monster_ids 收敛到场景里真实存在的敌意在场者
    encounter = data.get("encounter") or {}
    hostiles = {a["actor_id"]: a for a in hostile_actors(scene) if a.get("actor_id")}
    chosen = [mid for mid in (encounter.get("monster_ids") or []) if mid in hostiles]
    if not chosen:
        chosen = list(hostiles.keys())  # DM 没给或给错 → 全部敌意在场者参战
    if not chosen:  # 场景里压根没有敌人 → 无法开战，回落叙述
        return {
            "intent": "reply",
            "say": str(encounter.get("reason") or "这里并没有敌人。"),
            "world_writes": writes,
        }
    surprised = [sid for sid in (encounter.get("surprised") or []) if sid in chosen]
    return {
        "intent": "start_combat",
        "world_writes": writes,
        "encounter": {
            "monster_ids": chosen,
            "surprised": surprised,
            "reason": str(encounter.get("reason") or ""),
        },
    }


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
_COMBAT_WORDS = (
    "攻击",
    "开打",
    "动手",
    "拔剑",
    "砍",
    "杀",
    "战斗",
    "冲上去",
    "打它",
    "打他",
    "宣战",
    "attack",
)

# 检定关键词 → (属性, DC, kind)；命中即让玩家明骰
_CHECK_WORDS: list[tuple[tuple[str, ...], str, int, str]] = [
    (("撬锁", "开锁", "撬开"), Ability.DEXTERITY.value, 15, "ability_check"),
    (("说服", "劝说", "谈判", "游说"), Ability.CHARISMA.value, 13, "ability_check"),
    (("欺骗", "唬", "蒙骗"), Ability.CHARISMA.value, 13, "ability_check"),
    (("威吓", "恐吓"), Ability.CHARISMA.value, 13, "ability_check"),
    (
        ("搜索", "搜查", "调查", "查看", "检查", "翻找"),
        Ability.INTELLIGENCE.value,
        12,
        "ability_check",
    ),
    (
        ("察觉", "感知", "留意", "倾听", "察看"),
        Ability.WISDOM.value,
        12,
        "ability_check",
    ),
    (
        ("跳", "攀爬", "攀登", "推开", "搬", "掰", "破门"),
        Ability.STRENGTH.value,
        13,
        "ability_check",
    ),
    (("潜行", "躲", "藏", "溜"), Ability.DEXTERITY.value, 13, "ability_check"),
    (("豁免", "抵抗", "闪避"), Ability.DEXTERITY.value, 13, "saving_throw"),
]


def _decide_heuristic(user_input: str, scene: dict, party_ids: list[str]) -> dict:
    """关键词启发式：先看是否开战，再看是否检定，否则纯叙述。"""
    text = user_input or ""
    hostiles = hostile_actors(scene)

    # 1) 开战：有攻击意图且场上有敌意在场者
    if hostiles and any(w in text for w in _COMBAT_WORDS):
        return {
            "intent": "start_combat",
            "encounter": {
                "monster_ids": [a["actor_id"] for a in hostiles if a.get("actor_id")],
                "surprised": [sid for sid in (scene or {}).get("surprised", [])],
                "reason": "玩家发起攻击",
            },
        }

    # 2) 检定：命中关键词且有玩家角色可掷
    if party_ids:
        for keywords, ability, dc, kind in _CHECK_WORDS:
            if any(k in text for k in keywords):
                return {
                    "intent": "player_check",
                    "check": {
                        "actor_id": party_ids[0],
                        "ability": ability,
                        "dc": dc,
                        "kind": kind,
                        "proficient": False,
                        "prompt": f"请掷 d20（{ability} 检定，DC {dc}）",
                        "reason": f"你尝试「{text}」，结果尚不确定。",
                    },
                }

    # 3) 纯叙述：模板回应（离线占位，接 LLM 后被替换）
    loc = (scene or {}).get("location") or "此地"
    return {
        "intent": "reply",
        "say": f"（{loc}）你说：「{text}」。四下安静，故事在等你的下一步。",
    }


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


async def narrate_result(
    check_result: dict,
    *,
    use_llm: bool,
    action: str | None = None,
    scene: dict | None = None,
    messages: list[dict] | None = None,
    node_name: str = "dm",
) -> str:
    """叙述一次玩家检定的成败（成功→「是,然后…」，失败→「不,但是…」）。

    :param action: 玩家当时**尝试做的那件事**（来自检定的 prompt/reason），让叙述紧扣动作、
        而不是凭一个「成功/失败」凭空编一段不相干的画面。
    :param scene: 当前世界场景；让叙述对得上地点 / 在场者 / 气氛。
    :param messages: 最近对话；让叙述承接上文（玩家原话与上一段 DM 描述），保持连贯。
    """
    if use_llm:
        verdict = "成功" if check_result.get("success") else "失败"
        action_line = f"玩家当时尝试做的事：{action}\n" if action else ""
        scene_line = f"当前场景：{_dump(_scene_brief(scene))}\n" if scene else ""
        history_line = (
            f"最近对话：{_dump(_history_brief(messages))}\n" if messages else ""
        )
        task = (
            "玩家刚完成一次检定，结果已由引擎判定（既定事实，别改数字）：\n"
            f"{_dump(check_result)}\n"
            f"{action_line}"
            f"{scene_line}"
            f"{history_line}"
            f"判定为【{verdict}】。请用 1-3 句生动的中文叙述这个结果：**叙述要紧扣玩家当时尝试做的那件事，"
            "并与当前场景和上文连贯**（地点、气氛、在场者都要对得上），成功就「是，然后…」推进，"
            "失败就「不，但是…」给条出路，让故事继续。只描述结果，别罗列字段，别改判定数字。"
        )
        return await dm_narrate(task, node_name=node_name)
    # 离线模板
    ok = check_result.get("success")
    tail = "你做到了，事情顺势展开。" if ok else "没能成功，但门路并未完全堵死。"
    return narrate_reply(
        f"（检定{'成功' if ok else '失败'}：{check_result.get('total')} vs DC {check_result.get('dc')}）{tail}",
        node_name=node_name,
    )


async def narrate_aftermath(
    last_combat: dict, scene: dict, *, use_llm: bool, node_name: str = "dm"
) -> str:
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
    msg = (
        ("尘埃落定，你们赢了。" if won else "战斗失利……")
        + (f" 战利品：{loot}。" if loot else "")
        + " 接下来你打算怎么做？"
    )
    return narrate_reply(msg, node_name=node_name)


# ---------------------------------------------------------------------------
# 故事推进：语义是/否题 + 进入新拍的过场叙述
# ---------------------------------------------------------------------------
async def judge_trigger(
    prompt: str,
    scene: dict,
    *,
    user_input: str | None = None,
    messages: list[dict] | None = None,
    use_llm: bool,
) -> bool:
    """对一条**预写好的固定条件**问 DM 一道是/否题（窄判定，守住「结构归引擎」）。

    DM 只回答「到目前为止这条预设条件是否已为真」，**不裁定剧情走向**——把方差大的开放裁定
    收窄成可靠的二值判断（直接缓解需求文档「问题 1」）。离线/无模型时保守回落 ``False``
    （不推进，靠确定性触发 + 卡关兜底），避免误判跳拍。

    :param prompt: canon 里 semantic 触发器预写的判定问句。
    :param user_input: 玩家这步的原始言行（突出喂给 DM，避免它只盯着过期场景而漏判玩家刚做的事）。
    :return: 条件是否满足。
    """
    if not use_llm:
        return False
    action_line = f"玩家最新这步言行：{user_input}\n" if user_input else ""
    task = (
        "你在主持一场有预定剧本的 D&D 冒险。下面是一道**是/否判定题**，问的是「截至当前，某条预设的剧情推进条件是否已经为真」。\n"
        f"判定问题：{prompt}\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"最近对话：{_dump(_history_brief(messages or []))}\n"
        f"{action_line}\n"
        "判据：只依据**已经发生的玩家言行**判断，不要替玩家臆想他没做的事；但只要玩家已经用言语或行动"
        "表达出该条件描述的意图就应判为真——不必等玩家逐字复述。例如条件要求「决定动身前往某地」，"
        "玩家若已经起身朝那里走、潜行靠近、或明确说要去，即视为满足；别被「当前场景」仍停在原地点误导。\n"
        '**只输出一个 JSON 对象**：{"answer": true 或 false, "reason": "一句话依据"}。'
    )
    data = await dm_complete_json(task)
    if not isinstance(data, dict):
        logger.warning("[dm] judge_trigger 解析失败，保守判否 | prompt=%s", prompt)
        return False
    answer = bool(data.get("answer"))
    logger.info(
        "[judge_trigger] 「%s」→ %s | 依据=%s",
        prompt,
        "是" if answer else "否",
        data.get("reason", ""),
    )
    return answer


async def narrate_beat_transition(
    next_title: str,
    next_scene: dict,
    *,
    use_llm: bool,
    node_name: str = "dm",
) -> str:
    """叙述「进入新一拍（新珠子）」的过场：把镜头从上一颗珠子推到下一颗。"""
    if use_llm:
        task = (
            "故事推进到了新的一拍。请用 2-4 句生动的中文叙述这段过场，把玩家自然带入新场景，"
            "点出此地的气氛与可做的事，但**不要替玩家行动**，最后把决定权交回玩家。\n"
            f"新一拍标题：{next_title}\n"
            f"新场景：{_dump(_scene_brief(next_scene))}\n"
            "只描述场景与过渡，别罗列字段。"
        )
        return await dm_narrate(task, node_name=node_name)
    # 离线模板
    desc = (
        (next_scene or {}).get("description")
        or (next_scene or {}).get("location")
        or "新的场景"
    )
    return narrate_reply(
        f"【{next_title}】{desc} 你接下来打算怎么做？", node_name=node_name
    )
