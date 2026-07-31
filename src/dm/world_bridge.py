"""中央 DM 的「世界桥接」：把世界状态喂给 DM 智能体，并取回决策 / 叙述。

职责（**纯 DM 层**，依赖方向合规：本模块只依赖 ``src.dm`` 与 ``src.model``，不碰
``src.combat``——检定结算、中断构造、战斗输入装配等需要规则引擎的活，交给上层
``src.session`` 处理）：

- :func:`decide_turn` —— DM 读「场景 + 对话 + 玩家输入」，决定本回合意图：
  ``reply``（普通回应计划，可自掷暗骰）/ ``player_check``（要玩家明骰）/ ``start_combat``（开战）。
  必须走 :func:`dm_complete_json`；无模型或解析失败时直接报错，不允许离线模拟 DM。
- :func:`narrate_turn_final` ——
  汇总一个会话回合的裁定、结算与切拍结果，流式生成唯一玩家可见叙述（复用 custom 通道）。

决策只产出「规格」（如检定的 ability/dc/kind、遭遇的 monster_ids），**不计算加值、不判成败、
不组装战斗参战者**——那是引擎的事，放在 ``src.session``，以守住「规则归引擎」。
"""

from __future__ import annotations

import json
import logging

from src.common.utils.llm_util import ModelRole, get_model_name
from src.dm.agent import dm_complete_json, dm_narrate
from src.model.canon import TriggerKind
from src.model.dm_state import hostile_actors
from src.model.enums import Ability
from src.model.combatant import Combatant

logger = logging.getLogger(__name__)

# 合法属性值（校验 DM 给的 ability）
_ABILITY_VALUES = {a.value for a in Ability}
_CHECK_KINDS = {"ability_check", "saving_throw"}
_INTENTS = {"reply", "player_check", "start_combat", "use_action"}
_DECISION_ATTEMPTS = 3
_GUIDANCE_ATTEMPTS = 2
_WORLD_WRITE_FIELDS = {
    "flags_set",
    "moved_to",
    "clues_delivered",
    "discoveries",
    "transition_to_beat_id",
}


class WorldStateDecisionError(ValueError):
    """DM 决策引用了当前世界不允许写入的 flag、线索、地点或出口。"""


class WorldStateDecisionExhausted(RuntimeError):
    """真实 LLM 连续给出世界状态冲突，可交给会话图生成玩家引导。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# 上下文构造（喂给 DM 的最小画像，控延迟）
# ---------------------------------------------------------------------------
def _dump(obj) -> str:
    """紧凑 JSON（中文不转义）。"""
    return json.dumps(obj, ensure_ascii=False)


def _party_brief(party: dict[str, Combatant]) -> list[dict]:
    """玩家角色册压成最小画像，并公开可在探索中调用的已学技能 ID。"""
    return [
        {
            "id": c.id,
            "name": c.name,
            "hp": f"{c.current_hp}/{c.max_hp}",
            "class": getattr(c, "char_class", None),
            "class_id": getattr(c, "class_id", None),
            "alive": c.is_alive,
            "exploration_skill_ids": [
                skill.skill_id
                for skill in getattr(c, "skills", [])
                if "exploration" in skill.types
            ],
            "features": list(getattr(c, "features", [])),
            "inventory": [
                {
                    "item_id": item.item_id,
                    "quantity": item.quantity,
                }
                for item in getattr(c, "inventory", [])
            ],
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
            "combat_capable": bool(a.get("card")),
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
    active_actor_id: str | None = None,
    active_display_name: str | None = None,
) -> dict:
    """让 DM 决定本回合意图，返回规范化决策字典。

    返回形如::

        {"intent": "reply", "reply_brief": "本次回应计划/叙述要点"}
        {"intent": "player_check",
         "check": {"actor_id","ability","dc","kind","proficient","prompt","reason"}}
        {"intent": "start_combat",
         "encounter": {"monster_ids": [...], "surprised": [...], "reason": "..."}}
        {"intent": "use_action",
         "action": {"action_id": "...", "target_ids": ["..."]}}

    任一意图都可附带可选的世界写入声明 ``flags_set`` / ``moved_to`` / ``clues_delivered``
    （白名单校验由引擎在 evaluate_advancement 做），以及不改变世界状态的可选
    ``narrative_intent``，供最终叙述使用。

    :param beat_brief: 当前剧情拍骨架（目标/未传达线索/在场 NPC 目标秘密/出口提示），让叙述长在骨架上。
    :param stuck_hint: 卡关兜底指令（空转太久时注入），提示 DM 主动抛线索或指向出口。
    ``use_llm`` 参数保留给旧调用签名；DM 决策始终强制使用真实 LLM。
    """
    party_ids = list(party.keys())
    correction_hint = None
    last_error = ""
    locked_intent: str | None = None
    saw_recoverable_world_error = False
    saw_nonrecoverable_error = False
    for attempt in range(1, _DECISION_ATTEMPTS + 1):
        data = await _decide_llm(
            user_input,
            scene,
            party,
            messages or [],
            beat_brief,
            stuck_hint,
            active_actor_id,
            active_display_name,
            correction_hint=correction_hint,
        )
        if data is None:
            last_error = "LLM 未返回可解析 JSON"
            saw_nonrecoverable_error = True
            correction_hint = _decision_correction_hint(
                last_error,
                scene,
                party_ids,
                active_actor_id,
                beat_brief,
                locked_intent,
            )
            logger.warning(
                "[dm_decide] 第 %d 次决策不可解析，交回真实 LLM 重评", attempt
            )
            continue
        if locked_intent and data.get("intent") != locked_intent:
            last_error = (
                f"已锁定语义意图 {locked_intent!r}，不能降级为 "
                f"{data.get('intent')!r}"
            )
            saw_nonrecoverable_error = True
            correction_hint = _decision_correction_hint(
                last_error,
                scene,
                party_ids,
                active_actor_id,
                beat_brief,
                locked_intent,
            )
            logger.warning(
                "[dm_decide] 第 %d 次决策试图降级已锁定意图 | raw=%s",
                attempt,
                _dump(data),
            )
            continue
        try:
            return _normalize_decision(
                data,
                scene,
                party_ids,
                active_actor_id=active_actor_id,
                decision_context=beat_brief,
            )
        except ValueError as exc:
            last_error = str(exc)
            if isinstance(exc, WorldStateDecisionError):
                saw_recoverable_world_error = True
            else:
                saw_nonrecoverable_error = True
            if data.get("intent") == "start_combat":
                locked_intent = "start_combat"
            correction_hint = _decision_correction_hint(
                last_error,
                scene,
                party_ids,
                active_actor_id,
                beat_brief,
                locked_intent,
            )
            logger.warning(
                "[dm_decide] 第 %d 次决策不合法，交回真实 LLM 重评 | error=%s | raw=%s",
                attempt,
                last_error,
                _dump(data),
            )
    if saw_recoverable_world_error and not saw_nonrecoverable_error:
        raise WorldStateDecisionExhausted(last_error)
    raise RuntimeError(f"[dm] decide_turn LLM 连续输出非法决策：{last_error}")


async def _decide_llm(
    user_input,
    scene,
    party,
    messages,
    beat_brief=None,
    stuck_hint=None,
    active_actor_id=None,
    active_display_name=None,
    correction_hint=None,
) -> dict | None:
    """LLM 决策：拼最小上下文（含当前拍骨架）+ 严格 JSON 格式要求，调 DM 智能体。"""
    beat_line = (
        f"【当前剧情拍·只供你把控方向，勿照搬，尤其别直接抖出 NPC 秘密】{_dump(beat_brief)}\n"
        if beat_brief
        else ""
    )
    stuck_line = f"【推进提示】{stuck_hint}\n" if stuck_hint else ""
    correction_line = (
        f"【上次输出不合法，请重新裁定】{correction_hint}\n" if correction_hint else ""
    )
    task = (
        "你在主持一场有预定剧本(canon)的 D&D 冒险。请阅读当前局面，决定如何回应玩家这一步，并**只输出一个 JSON 对象**。\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"玩家队伍：{_dump(_party_brief(party))}\n"
        f"最近对话：{_dump(_history_brief(messages))}\n"
        f"{beat_line}"
        f"{stuck_line}"
        f"{correction_line}"
        f"当前发言玩家：{active_display_name or '未知'}；其角色 actor_id：{active_actor_id or '未知'}。"
        "如果这一步需要玩家检定，actor_id 必须使用当前发言玩家的角色。\n"
        f"玩家这一步说/做：{user_input}\n\n"
        "叙述要自然朝当前拍目标推进但不硬拽玩家；你无权跳拍或改写骨架，推进由引擎判定。\n"
        "剧情拍骨架中的 known_clues 是玩家跨剧情拍持续掌握的既有事实；回应时必须保持一致，"
        "可在相关时自然引用其受控正文，但不得把 available_discoveries 等尚未发现内容当作已知事实。\n"
        "若输出 reply，不要生成玩家可见正文，只输出 reply_brief：一句话说明本次回应要点，"
        "可包含应传达的事实、NPC 态度、线索提示和建议的行动方向；玩家可见叙述会由后续流式叙述节点生成。\n"
        "判断本步属于以下哪一类（判据见你的系统提示）：\n"
        "1) 纯叙事/社交/信息，或该掷暗骰（陷阱、对抗、环境）——你可调骰子工具自己掷，"
        '把既定结果和回应要点写入 reply_brief，输出 {"intent":"reply","reply_brief":"一句话回应计划"}。\n'
        "2) 玩家主动做一件结果不确定且成败都有意义的事（撬锁/说服/跳跃/豁免…）——交玩家明骰，"
        '输出 {"intent":"player_check","check":{"actor_id":"哪个玩家角色id","ability":"strength|dexterity|constitution|intelligence|wisdom|charisma",'
        '"dc":数字,"kind":"ability_check|saving_throw","proficient":true/false,"prompt":"提示玩家掷什么","reason":"为什么要检定"}}。\n'
        '3) 局势升级为战斗——预置遭遇输出 {"intent":"start_combat","encounter":{"encounter_id":"候选遭遇id",'
        '"target_actor_ids":["目标actor_id"],"surprised":["被突袭者id"],"reason":"..."},'
        '"before_combat":{"transition_to_beat_id":"必要时填写目标拍"}}；当前场景临时冲突可省略 encounter_id。\n'
        "玩家明确攻击可接触且有战斗卡面的目标时，必须选择 start_combat，不要再次要求确认；"
        "不要提前叙述命中、伤害或受伤。\n"
        "4) 玩家明确使用 available_actions 中的技能、物品或任务特性——输出 "
        '{"intent":"use_action","action":{"action_id":"合法 action_id",'
        '"target_ids":["仅从该行动 targets 中选择"]}}。只能选择 enabled=true 的行动；'
        "不能自行结算检定、物品消耗或效果。\n"
        "【可选·世界写入】当玩家这步确实改变了世界时，可在 JSON 里附带（不改变上面的 intent）：\n"
        '  "flags_set":{"flag名":true} —— 仅声明 canon 白名单内、且不由线索发现效果管理的普通世界 flag；\n'
        '  "moved_to":"地点id" —— 玩家移动到的当前拍内地点；\n'
        '  "clues_delivered":["你这步已讲给玩家的关键线索id"]；\n'
        '  "discoveries":["玩家这步真正发现/取得的线索id"] —— 线索对应 flag 与物品只能用此字段触发；\n'
        "discoveries 只能填写 available_discoveries 中仍可发现的线索 id，绝不能填写 flag 名；"
        "managed_flag_sources 中的 flag 由引擎写入，不能放进 flags_set。"
        "若 current_flags、discovered_clue_ids 或角色 inventory 已表明状态完成，不要重复声明写入。\n"
        '  "transition_to_beat_id":"玩家已经完成的合法跨拍行动目标" —— 只能从 '
        'reachable_transitions 中 trigger_kind="action" 的目标选择；semantic 等其它出口由引擎判定，'
        "不得直接写入。\n"
        "若 player_check 成功后才发生世界变化，必须把上述字段放进 "
        '"effects":{"on_success":{...},"on_failure":{...}}，不可提前写入。\n'
        "如果一次行动在检定成功后立即开战，可在 on_success 里同时给出 "
        '"start_combat":{"encounter_id":"...","target_actor_ids":[...],"reason":"..."}。\n'
        "每种意图都可附带可选 narrative_intent：用一句话规划一处伏笔、意象、潜台词或细微反应；"
        "不得借此新增关键事实、人物、道具、线索或规则结果。\n"
        "不确定 DC 时可 kb_read ability_check / 即兴伤害表。只输出 JSON，不要额外文字。"
    )
    return await dm_complete_json(
        task,
        model_name=get_model_name(ModelRole.DM_DECISION),
    )


def _decision_correction_hint(
    error: str,
    scene: dict,
    party_ids: list[str],
    active_actor_id: str | None = None,
    decision_context: dict | None = None,
    locked_intent: str | None = None,
) -> str:
    """把非法决策反馈给真实 LLM，要求它重新产出合法 JSON。"""
    hostiles = [
        {
            "actor_id": actor.get("actor_id"),
            "name": actor.get("name"),
            "disposition": actor.get("disposition"),
        }
        for actor in hostile_actors(scene)
        if actor.get("actor_id")
    ]
    reachable = (decision_context or {}).get("reachable_encounters", [])
    action_transitions = sorted(_allowed_action_transition_ids(decision_context))
    context = decision_context or {}
    available_discoveries = context.get("available_discoveries", [])
    allowed_delivery_ids = context.get("allowed_delivery_clue_ids", [])
    allowed_discovery_ids = context.get("allowed_discovery_clue_ids", [])
    managed_sources = context.get("managed_flag_sources", {})
    intent_line = (
        "上次已确认玩家明确开战，本次 intent 必须保持 start_combat，只修正引用和字段；"
        if locked_intent == "start_combat"
        else ""
    )
    return (
        f"错误原因：{error}。"
        f"合法玩家 actor_id：{_dump(party_ids)}。"
        f"当前发言角色 actor_id：{active_actor_id or '未知'}。"
        f"当前场景合法敌意 actor_id：{_dump(hostiles)}。"
        f"可达预置遭遇：{_dump(reachable)}。"
        f"可显式写入的 action 跨拍目标：{_dump(action_transitions)}。"
        f"当前世界 flags：{_dump(context.get('current_flags', {}))}。"
        f"已经发现的线索 id：{_dump(context.get('discovered_clue_ids', []))}。"
        f"合法 clues_delivered id：{_dump(allowed_delivery_ids)}。"
        f"合法 discoveries id 与原子效果：{_dump(available_discoveries)}。"
        f"本次 discoveries 只能从 {_dump(allowed_discovery_ids)} 选择。"
        f"引擎管理 flag 及来源：{_dump(managed_sources)}。"
        f"{intent_line}"
        "start_combat 只能引用当前有卡面的 actor 或可达预置遭遇；"
        "semantic 等非 action 出口必须交给引擎判定，不得写 transition_to_beat_id；"
        "discoveries 绝不能填写 flag 名；引擎管理 flag 绝不能放入 flags_set；"
        "若状态已完成或玩家本步没有形成新的合法写入，就省略对应字段；"
        "如果错误是 reply_brief 缺失，请补上一句不可见回应计划，说明该传达什么和应引导玩家做什么。"
        "不要编造不存在的 actor_id。只输出修正后的 JSON。"
    )


async def plan_world_state_guidance(
    user_input: str,
    scene: dict,
    party: dict[str, Combatant],
    *,
    issue: str,
    messages: list[dict] | None = None,
    beat_brief: dict | None = None,
    stuck_hint: str | None = None,
) -> dict:
    """由真实 LLM 为无法落地的玩家行动规划一段不写世界状态的引导。"""
    correction = ""
    for attempt in range(1, _GUIDANCE_ATTEMPTS + 1):
        task = (
            "你在主持一场有预定剧本的 D&D 冒险。玩家本步行动与当前世界状态发生冲突，"
            "不能照原计划提交世界变化。请给后续叙述节点一条自然、可执行的引导计划，"
            "帮助玩家通过自己的下一步行动继续冒险。\n"
            f"当前场景：{_dump(_scene_brief(scene))}\n"
            f"玩家队伍与背包：{_dump(_party_brief(party))}\n"
            f"当前剧情拍与合法候选：{_dump(beat_brief or {})}\n"
            f"最近对话：{_dump(_history_brief(messages or []))}\n"
            f"玩家本步言行：{user_input}\n"
            f"内部冲突原因（只供裁定，不得向玩家展示字段名或技术错误）：{issue}\n"
            f"额外推进提示：{stuck_hint or '无'}\n"
            "只可依据 available_discoveries、当前地点、advance_hints、合法出口和玩家现有背包提出办法。"
            "若某个尚未发现的线索能补齐所需物品，应自然提示玩家去对应人物、遗体或地点调查；"
            "若 Canon 已给出另一条合法路线，也可以提示尝试该路线。"
            "不要声称玩家已经拿到物品、发现线索、移动成功或完成推进；本回合只提示，"
            "真正的 discoveries 和其它世界写入必须等待玩家下一次明确行动。"
            "不得编造新钥匙、新 NPC、新入口或未授权规则。\n"
            "只输出 JSON："
            '{"reply_brief":"一句话说明该告诉玩家什么、可尝试哪些下一步",'
            '"narrative_intent":"可选的一句意象或潜台词"}。'
            f"{correction}"
        )
        data = await dm_complete_json(
            task,
            model_name=get_model_name(ModelRole.DM_GUIDANCE),
        )
        if data is None:
            correction = "\n上次输出不可解析；请严格只返回指定 JSON。"
        else:
            illegal_writes = sorted(_WORLD_WRITE_FIELDS & set(data))
            reply_brief = (
                str(data.get("reply_brief") or "").strip()
                if isinstance(data, dict)
                else ""
            )
            if reply_brief and not illegal_writes:
                narrative_intent = (
                    str(data.get("narrative_intent") or "").strip()
                    if isinstance(data, dict)
                    else ""
                )
                return {
                    "reply_brief": reply_brief,
                    "narrative_intent": narrative_intent,
                }
            correction = (
                "\n上次引导计划不合法："
                + (
                    f"不得包含世界写入字段 {illegal_writes}；"
                    if illegal_writes
                    else "缺少非空 reply_brief；"
                )
                + "请只规划提示，不修改世界状态。"
            )
        logger.warning(
            "[dm_guidance] 第 %d 次引导计划不合法，交回真实 LLM 重评",
            attempt,
        )
    raise RuntimeError("[dm] 世界状态冲突引导 LLM 连续输出非法计划")


def _allowed_action_transition_ids(
    decision_context: dict | None,
) -> set[str]:
    """提取允许 DM 显式提交的 action 跨拍目标。"""
    context = decision_context or {}
    return {
        str(item["to_beat_id"])
        for item in context.get("reachable_transitions", [])
        if item.get("trigger_kind") == TriggerKind.ACTION.value
        and item.get("to_beat_id")
    }


def _world_writes(data: dict, decision_context: dict | None = None) -> dict:
    """抽取并校验 DM 世界写入，只接受当前拍明确提供的候选 id。"""
    context = decision_context or {}
    allowed_flags = set(context.get("allowed_flags", []))
    managed_flags = set((context.get("managed_flag_sources") or {}).keys())
    allowed_delivery_clues = set(context.get("allowed_delivery_clue_ids", []))
    allowed_discovery_clues = set(context.get("allowed_discovery_clue_ids", []))
    allowed_locations = {item.get("id") for item in context.get("locations", [])}
    allowed_transitions = _allowed_action_transition_ids(context)
    writes: dict = {}
    flags_set = data.get("flags_set")
    if isinstance(flags_set, dict) and flags_set:
        normalized_flags = {str(k): v for k, v in flags_set.items()}
        invalid = (
            set(normalized_flags) - allowed_flags
            if "allowed_flags" in context
            else set()
        )
        if invalid:
            raise WorldStateDecisionError(
                f"[dm] flags_set 含非法 flag：{sorted(invalid)}"
            )
        managed = set(normalized_flags) & managed_flags
        if managed:
            raise WorldStateDecisionError(
                f"[dm] 引擎管理 flag 不能由 DM 直接写入：{sorted(managed)}"
            )
        writes["flags_set"] = normalized_flags
    moved_to = data.get("moved_to")
    if isinstance(moved_to, str) and moved_to:
        if "locations" in context and moved_to not in allowed_locations:
            raise WorldStateDecisionError(
                f"[dm] moved_to 不在当前拍地点中：{moved_to!r}"
            )
        writes["moved_to"] = moved_to
    clues = data.get("clues_delivered")
    if isinstance(clues, list) and clues:
        normalized_clues = [str(c) for c in clues]
        invalid = (
            set(normalized_clues) - allowed_delivery_clues
            if "allowed_delivery_clue_ids" in context
            else set()
        )
        if invalid:
            raise WorldStateDecisionError(
                f"[dm] clues_delivered 含非法线索：{sorted(invalid)}"
            )
        writes["clues_delivered"] = normalized_clues
    discoveries = data.get("discoveries")
    if isinstance(discoveries, list) and discoveries:
        normalized_discoveries = [str(c) for c in discoveries]
        invalid = (
            set(normalized_discoveries) - allowed_discovery_clues
            if "allowed_discovery_clue_ids" in context
            else set()
        )
        if invalid:
            raise WorldStateDecisionError(
                f"[dm] discoveries 含非法线索：{sorted(invalid)}"
            )
        writes["discoveries"] = normalized_discoveries
    transition_to = data.get("transition_to_beat_id")
    if isinstance(transition_to, str) and transition_to:
        if transition_to not in allowed_transitions:
            raise WorldStateDecisionError(
                f"[dm] transition_to_beat_id 不是合法 action 出口：{transition_to!r}"
            )
        writes["transition_to_beat_id"] = transition_to
    return writes


def _normalize_decision(
    data: dict,
    scene: dict,
    party_ids: list[str],
    *,
    active_actor_id: str | None = None,
    decision_context: dict | None = None,
) -> dict:
    """校验并规范化 DM 给的决策；非法字段直接报错。

    任一意图都会带上 ``world_writes`` 字段（可能为空 dict），承载 DM 声明的世界变化。
    """
    writes = _world_writes(data, decision_context)
    intent = data.get("intent")
    if intent not in _INTENTS:
        raise ValueError(f"[dm] 非法 DM 意图：{intent!r}")
    narrative_intent = (
        data.get("narrative_intent", "").strip()
        if isinstance(data.get("narrative_intent"), str)
        else ""
    )

    if intent == "reply":
        reply_brief = str(data.get("reply_brief") or "").strip()
        if not reply_brief:
            raise ValueError("[dm] reply 未给出 reply_brief")
        return {
            "intent": "reply",
            "reply_brief": reply_brief,
            "narrative_intent": narrative_intent,
            "world_writes": writes,
        }

    if intent == "use_action":
        if writes:
            raise WorldStateDecisionError(
                "[dm] use_action 的世界变化只能由规则行动执行器产生"
            )
        raw_action = data.get("action") or {}
        action_id = str(raw_action.get("action_id") or "")
        actions = list((decision_context or {}).get("available_actions", []))
        selected = next(
            (
                item
                for item in actions
                if item.get("action_id") == action_id and item.get("enabled")
            ),
            None,
        )
        if selected is None:
            raise ValueError(f"[dm] use_action 引用了当前不可用行动：{action_id!r}")
        target_ids = list(
            dict.fromkeys(str(value) for value in raw_action.get("target_ids", []))
        )
        legal_targets = {
            str(target.get("id"))
            for target in selected.get("targets", [])
            if target.get("id")
        }
        if not (
            int(selected.get("min_targets", 0))
            <= len(target_ids)
            <= int(selected.get("max_targets", 20))
        ) or not set(target_ids).issubset(legal_targets):
            raise ValueError("[dm] use_action 的目标不合法")
        return {
            "intent": "use_action",
            "narrative_intent": narrative_intent,
            "world_writes": {},
            "action": {"action_id": action_id, "target_ids": target_ids},
        }

    if intent == "player_check":
        premature = set(writes) & {
            "moved_to",
            "transition_to_beat_id",
            "discoveries",
        }
        if premature:
            raise WorldStateDecisionError(
                "[dm] player_check 的移动、跨拍和发现效果必须放入 "
                f"effects.on_success/on_failure：{sorted(premature)}"
            )
        check = data.get("check") or {}
        ability = check.get("ability")
        if ability not in _ABILITY_VALUES:
            ability = Ability.DEXTERITY.value
        actor_id = check.get("actor_id")
        if actor_id not in party_ids:
            raise ValueError(f"[dm] 检定 actor_id 不在队伍中：{actor_id!r}")
        if active_actor_id and actor_id != active_actor_id:
            raise ValueError(
                "[dm] 检定 actor_id 必须是当前发言玩家角色：" f"{active_actor_id!r}"
            )
        kind = (
            check.get("kind") if check.get("kind") in _CHECK_KINDS else "ability_check"
        )
        return {
            "intent": "player_check",
            "narrative_intent": narrative_intent,
            "world_writes": writes,
            "effects": _normalize_check_effects(
                data.get("effects"), scene, party_ids, decision_context
            ),
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

    before_combat = dict(data.get("before_combat") or {})
    if writes.get("transition_to_beat_id") and not before_combat.get(
        "transition_to_beat_id"
    ):
        before_combat["transition_to_beat_id"] = writes["transition_to_beat_id"]
    encounter = _normalize_encounter(
        data.get("encounter") or {},
        before_combat,
        scene,
        party_ids,
        decision_context,
    )
    return {
        "intent": "start_combat",
        "narrative_intent": narrative_intent,
        "world_writes": writes,
        "encounter": encounter,
    }


def _normalize_check_effects(
    raw: object,
    scene: dict,
    party_ids: list[str],
    decision_context: dict | None,
) -> dict:
    """规范化检定成功/失败后的条件化世界效果。"""
    effects = raw if isinstance(raw, dict) else {}
    normalized: dict = {}
    for branch_name in ("on_success", "on_failure"):
        branch = effects.get(branch_name)
        if not isinstance(branch, dict):
            continue
        item: dict = {
            "world_writes": _world_writes(branch, decision_context),
        }
        start_combat = branch.get("start_combat")
        if isinstance(start_combat, dict):
            before = {"transition_to_beat_id": branch.get("transition_to_beat_id")}
            item["combat_request"] = _normalize_encounter(
                start_combat,
                before,
                scene,
                party_ids,
                decision_context,
            )
        normalized[branch_name] = item
    return normalized


def _normalize_encounter(
    raw: dict,
    before_combat: dict,
    scene: dict,
    party_ids: list[str],
    decision_context: dict | None,
) -> dict:
    """把 DM 战斗请求绑定到当前 actor 或 canon 提供的预置遭遇。"""
    context = decision_context or {}
    current_actor_ids = {
        actor.get("actor_id")
        for actor in (scene or {}).get("actors", [])
        if actor.get("actor_id") and actor.get("card")
    }
    candidates = list(context.get("reachable_encounters", []))
    current_encounter = context.get("current_encounter")
    if current_encounter:
        candidates.append(current_encounter)
    candidate_by_id = {
        item.get("encounter_id"): item
        for item in candidates
        if item.get("encounter_id")
    }

    encounter_id = raw.get("encounter_id")
    requested_ids = [
        str(value)
        for value in (raw.get("target_actor_ids") or raw.get("monster_ids") or [])
    ]
    candidate = candidate_by_id.get(encounter_id) if encounter_id else None
    if encounter_id and candidate is None:
        raise ValueError(f"[dm] start_combat 引用了非法 encounter_id：{encounter_id!r}")

    if candidate is None and requested_ids:
        matching = [
            item
            for item in candidates
            if set(requested_ids).issubset(set(item.get("monster_ids", [])))
        ]
        if len(matching) == 1:
            candidate = matching[0]
            encounter_id = candidate.get("encounter_id")

    if candidate is not None:
        allowed_targets = set(candidate.get("monster_ids", []))
        chosen = requested_ids or list(allowed_targets)
        if not chosen or not set(chosen).issubset(allowed_targets):
            raise ValueError("[dm] start_combat 目标不属于所选预置遭遇")
    else:
        chosen = requested_ids
        if not chosen or not set(chosen).issubset(current_actor_ids):
            raise ValueError("[dm] start_combat 未给出当前有卡面的 actor")

    allowed_transitions = _allowed_action_transition_ids(context)
    transition_to = before_combat.get("transition_to_beat_id")
    if candidate and candidate.get("beat_id") != context.get("beat_id"):
        transition_to = transition_to or candidate.get("beat_id")
    if transition_to and transition_to not in allowed_transitions:
        raise WorldStateDecisionError(
            f"[dm] 开战前迁移不是合法 action 出口：{transition_to!r}"
        )

    participants = set(chosen) | set(party_ids)
    surprised = [
        str(value)
        for value in (raw.get("surprised") or raw.get("surprised_actor_ids") or [])
        if str(value) in participants
    ]
    return {
        "encounter_id": encounter_id,
        "source": "canon_encounter" if encounter_id else "scene_actors",
        "target_actor_ids": chosen,
        "monster_ids": chosen,
        "surprised": surprised,
        "reason": str(raw.get("reason") or ""),
        "before_combat": (
            {"transition_to_beat_id": transition_to} if transition_to else {}
        ),
    }


def _safe_int(value, default: int) -> int:
    """容错取整。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 叙述：把要对玩家说的话推给前端
# ---------------------------------------------------------------------------
async def narrate_reply_llm(
    reply_brief: str,
    scene: dict,
    *,
    user_input: str | None = None,
    messages: list[dict] | None = None,
    beat_brief: dict | None = None,
    stuck_hint: str | None = None,
    use_llm: bool,
    node_name: str = "dm",
) -> str:
    """根据 reply 决策计划流式生成玩家可见叙述。

    reply_brief 是上一阶段 DM 决策产出的不可见计划；本函数负责把它扩写为真正给玩家看的文本。
    ``use_llm`` 参数保留给调用签名一致性；DM 叙述始终强制使用真实 LLM。
    """
    action_line = f"玩家最新这步言行：{user_input}\n" if user_input else ""
    beat_line = (
        f"当前剧情拍骨架（只供把控方向，勿照搬）：{_dump(beat_brief)}\n"
        if beat_brief
        else ""
    )
    stuck_line = f"推进提示：{stuck_hint}\n" if stuck_hint else ""
    task = (
        "你在主持一场有预定剧本的 D&D 冒险。上一阶段已经完成意图裁定，"
        "现在请把 reply 计划写成玩家可见叙述。\n"
        f"reply 计划：{reply_brief}\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"最近对话：{_dump(_history_brief(messages or []))}\n"
        f"{action_line}"
        f"{beat_line}"
        f"{stuck_line}"
        "要求：用 2-4 句自然中文回应玩家，少铺陈，不替玩家行动；"
        "必须承接玩家这一步和当前场景，可传达 reply 计划里的线索或 NPC 态度。"
        "普通收尾自然交还控制权；只有路线、风险或资源出现关键分支，或者玩家明显卡住时，"
        "才简洁提示最相关的行动方向，不要固定输出「你可以……」菜单。"
        "只输出玩家可见叙述，不要输出 JSON，不要罗列字段。"
    )
    return await dm_narrate(
        task,
        model_name=get_model_name(ModelRole.DM_NARRATION),
        node_name=node_name,
    )


async def narrate_turn_final(
    *,
    user_input: str | None,
    reply_brief: str | None,
    narrative_intent: str | None,
    last_check: dict | None,
    last_combat: dict | None,
    previous_scene: dict | None,
    scene: dict,
    beat_brief: dict | None,
    story_transition: dict | None,
    messages: list[dict] | None,
    use_llm: bool,
    node_name: str = "dm",
) -> str:
    """统一叙述一个玩家回合的最终结果，确保本回合只产生一条玩家可见 DM 消息。

    参数里的事实均由上游节点裁定或结算完成；本函数只负责把它们讲成玩家可见叙述。
    ``narrative_intent`` 只允许影响表达，``beat_brief`` 提供 canon 与关键 NPC 死亡续接边界。
    ``use_llm`` 参数保留给调用签名一致性；DM 叙述始终强制使用真实 LLM。
    """
    transition = story_transition or {"type": "stay"}
    transition_type = transition.get("type")
    previous_line = (
        f"切换前场景：{_dump(_scene_brief(previous_scene))}\n" if previous_scene else ""
    )
    check_line = f"本回合检定结果：{_dump(last_check)}\n" if last_check else ""
    combat_line = f"最近战斗结果：{_dump(last_combat)}\n" if last_combat else ""
    reply_line = f"DM 回应计划：{reply_brief}\n" if reply_brief else ""
    intent_line = f"DM 隐藏叙事意图：{narrative_intent}\n" if narrative_intent else ""
    beat_line = (
        f"当前剧情拍骨架（只供把控方向，不得直接泄露秘密）：{_dump(beat_brief)}\n"
        if beat_brief
        else ""
    )
    action_line = f"玩家最新这步言行：{user_input}\n" if user_input else ""

    if transition_type == "advance":
        instruction = (
            "本回合已经触发剧情推进并切换到了新场景。请先承接玩家动作或结算结果，"
            "再自然描述从旧场景到新场景的过渡，最后把镜头落在当前新场景的可见要素和可行动方向上。"
        )
    else:
        instruction = (
            "本回合没有切换剧情拍。请基于当前场景、玩家动作、回应计划和已有结算，"
            "描述当前场景内发生的结果，并把选择权交还给玩家。"
        )

    task = (
        "你在主持一场有预定剧本的 D&D 冒险。下面是本回合已经由引擎和 DM 决策节点确定的事实，"
        "请统一生成本回合唯一一段玩家可见叙述。\n"
        f"{action_line}"
        f"{reply_line}"
        f"{intent_line}"
        f"{check_line}"
        f"{combat_line}"
        f"{previous_line}"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        f"{beat_line}"
        f"故事推进摘要：{_dump(transition)}\n"
        f"最近对话：{_dump(_history_brief(messages or []))}\n"
        f"叙述策略：{instruction}\n"
        "要求：用 2-4 句自然中文，少铺陈，不替玩家行动；只描述既定事实，不新增规则数字，"
        "不改写检定、战斗或剧情推进结果。可以加入最多一处不改变世界状态的伏笔、意象、潜台词或"
        "细微反应；不得泄露 NPC 秘密，也不得凭空创造关键人物、可交互道具或关键线索。"
        "若最近战斗结果包含 automatic_discoveries，它们已由引擎完成发现、传达与物品发放："
        "必须把翻检战败敌人、线索正文和实际获得物品作为既定事实自然写入战后叙述，"
        "不得再让玩家选择是否搜身，也不得把发放交给 DM 裁定。"
        "若剧情骨架列出 critical_npc_deaths，死者不得重新行动或说话；应在相关时刻体现 consequence，"
        "并按 guidance 给出继续调查的入口，但不能仅靠叙述自动授予线索 flag、物品或剧情推进。"
        "普通收尾自然交还控制权，不必列行动菜单；只有真正影响路线、风险或资源的关键分支，"
        "或者上下文显示玩家卡住时，才简洁提示必要方向，且无需以固定措辞开头。"
        "只输出玩家可见叙述，不要输出 JSON，不要罗列字段。"
    )
    return await dm_narrate(
        task,
        model_name=get_model_name(ModelRole.DM_NARRATION),
        node_name=node_name,
    )


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
    verdict = "成功" if check_result.get("success") else "失败"
    action_line = f"玩家当时尝试做的事：{action}\n" if action else ""
    scene_line = f"当前场景：{_dump(_scene_brief(scene))}\n" if scene else ""
    history_line = f"最近对话：{_dump(_history_brief(messages))}\n" if messages else ""
    task = (
        "玩家刚完成一次检定，结果已由引擎判定（既定事实，别改数字）：\n"
        f"{_dump(check_result)}\n"
        f"{action_line}"
        f"{scene_line}"
        f"{history_line}"
        f"判定为【{verdict}】。请用 1-3 句生动的中文叙述这个结果：**叙述要紧扣玩家当时尝试做的那件事，"
        "并与当前场景和上文连贯**（地点、气氛、在场者都要对得上），成功就「是，然后…」推进，"
        "失败就「不，但是…」给条出路，让故事继续。只有结果形成关键路线、风险或资源分支时才提示方向，"
        "不要固定输出行动菜单。"
        "只描述结果，别罗列字段，别改判定数字。"
    )
    return await dm_narrate(
        task,
        model_name=get_model_name(ModelRole.DM_NARRATION),
        node_name=node_name,
    )


async def narrate_aftermath(
    last_combat: dict, scene: dict, *, use_llm: bool, node_name: str = "dm"
) -> str:
    """战斗结束后，叙述战后世界（谁倒下、战利品、接下来），把故事交回 DM。"""
    task = (
        "一场战斗刚结束，结果已由战斗引擎结算（既定事实）：\n"
        f"{_dump(last_combat)}\n"
        f"当前场景：{_dump(_scene_brief(scene))}\n"
        "请用 2-4 句中文收尾这场战斗：胜负、伤亡和已经结算的收获，并自然地把镜头交回玩家。"
        "automatic_discoveries 中的搜身、线索和物品已经自动完成，必须直接叙述，不得再次建议搜尸。"
        "只有路线、风险或资源出现关键分支时才简洁提示方向，不要固定输出行动菜单。"
        "只描述既定结果，别新增战斗数字。"
    )
    return await dm_narrate(
        task,
        model_name=get_model_name(ModelRole.DM_NARRATION),
        node_name=node_name,
    )


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
    收窄成可靠的二值判断（直接缓解需求文档「问题 1」）。本判定必须由真实 LLM 完成。

    :param prompt: canon 里 semantic 触发器预写的判定问句。
    :param user_input: 玩家这步的原始言行（突出喂给 DM，避免它只盯着过期场景而漏判玩家刚做的事）。
    :return: 条件是否满足。
    """
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
    data = await dm_complete_json(
        task,
        model_name=get_model_name(ModelRole.DM_TRIGGER),
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"[dm] judge_trigger LLM 未返回可解析 JSON：{prompt}")
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
    task = (
        "故事推进到了新的一拍。请用 2-4 句生动的中文叙述这段过场，把玩家自然带入新场景，"
        "点出此地的气氛与关键可见要素，但**不要替玩家行动**。只有路线、风险或资源出现关键分支时，"
        "才简洁提示必要方向，不要固定输出行动菜单。\n"
        f"新一拍标题：{next_title}\n"
        f"新场景：{_dump(_scene_brief(next_scene))}\n"
        "只描述场景与过渡，别罗列字段。"
    )
    return await dm_narrate(
        task,
        model_name=get_model_name(ModelRole.DM_NARRATION),
        node_name=node_name,
    )
