"""剧情圣经 `Canon` 及其结构性校验、确定性触发判定（graph-free 纯数据 + 纯函数）。

落实 docs/故事框架/00-故事系统需求分析.md 第三~四节：把「提前定死的剧本骨架」表达成只读数据。
形状是**糖葫芦**——主线是一串基本线性的「珠子」（`Beat`），每颗珠子内部是可自由探索的小沙盒，
只有触动「推进条件」（`Trigger`）才串到下一颗，从而保证一局冒险**一定有头有尾、一定能结束**。

铁律「结构归引擎」：拍与拍之间的推进只能由引擎依据 canon 的推进条件判定，DM 无权跳拍或改写骨架。
本模块属 ``model`` 层：**不依赖** LangGraph / combat / dm / session，只放数据形状与纯函数。
英文标识符 + 中文注释，沿用 combatant.py 的 ``@dataclass(slots=True)`` + ``from_dict`` 模式。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re
from typing import Any

from src.model.combatant import Combatant
from src.model.enums import StrEnum
from src.model.rule_action import ActionDefinition

_CANON_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# 故事域枚举（值即落 JSON / 上前端的字符串，沿用 enums.py 的 StrEnum 约定）
# ---------------------------------------------------------------------------
class BeatKind(StrEnum):
    """一拍的类型，对应流程图四段。"""

    OPENING = "opening"  # 开场·任务引入
    EXPLORATION = "exploration"  # 探索（珠内自由沙盒）
    CONFLICT = "conflict"  # 冲突（小遭遇）
    CLIMAX = "climax"  # 高潮·Boss 决战
    ENDING = "ending"  # 结局·收尾


class TriggerKind(StrEnum):
    """推进条件的判定方式：前四种由引擎确定性判定，semantic 留给 DM 是/否题兜底。"""

    FLAG = "flag"  # 世界 flag 为某值（引擎确定）
    ITEM = "item"  # 队伍持有某道具（引擎确定）
    LOCATION = "location"  # 玩家到达某地点（引擎确定）
    COMBAT_OUTCOME = "combat_outcome"  # 某场战斗的结果（引擎确定）
    SEMANTIC = "semantic"  # 对一条预写固定条件问 DM 是/否（兜底）
    ACTION = "action"  # 仅由 DM 已确认并经引擎校验的结构化行动触发


class EndingOutcome(StrEnum):
    """结局拍的归属：胜局 / 败局。"""

    WIN = "win"  # 胜利结局
    LOSE = "lose"  # 失败结局


# ---------------------------------------------------------------------------
# 子结构
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Trigger:
    """一个推进条件。``kind`` 决定判定方式，``predicate`` 携带判定参数。"""

    id: str  # 触发器 id（出口据此引用）
    kind: TriggerKind  # 判定方式
    predicate: dict[str, Any] = field(
        default_factory=dict
    )  # 判定参数（见 evaluate_trigger）
    description: str = ""  # 一句话说明（喂给 DM 的出口提示 / semantic 问句）

    @classmethod
    def from_dict(cls, data: dict) -> "Trigger":
        """从字典构造触发器。"""
        return cls(
            id=data["id"],
            kind=TriggerKind(data["kind"]),
            predicate=dict(data.get("predicate", {})),
            description=str(data.get("description", "")),
        )


@dataclass(slots=True)
class Exit:
    """一个出口：某触发器命中后通向哪一拍。"""

    trigger_id: str  # 命中即走此出口的触发器 id
    next_beat_id: str  # 通向的下一拍 id

    @classmethod
    def from_dict(cls, data: dict) -> "Exit":
        """从字典构造出口。"""
        return cls(trigger_id=data["trigger_id"], next_beat_id=data["next_beat_id"])


@dataclass(slots=True)
class KeyInfo:
    """本拍 DM **必须**让玩家获知的关键线索。「是否已传达」记在 ``story.delivered_clues``，不入只读 canon。"""

    id: str  # 线索 id
    text: str  # 线索内容（DM 要把它自然地讲给玩家）
    location_id: str | None = (
        None  # 新 Canon 明确绑定可发现地点；旧 Canon 缺省为整拍可见
    )
    discovery_hints: list[str] = field(default_factory=list)  # 可执行的接近/发现方式
    discovery_effects: dict[str, Any] = field(
        default_factory=dict
    )  # 玩家真正发现后由引擎提交的 flag / 物品效果

    @classmethod
    def from_dict(cls, data: dict) -> "KeyInfo":
        """从字典构造关键线索。"""
        return cls(
            id=data["id"],
            text=str(data.get("text", "")),
            location_id=(
                str(data["location_id"])
                if data.get("location_id") is not None
                else None
            ),
            discovery_hints=[str(item) for item in data.get("discovery_hints", [])],
            discovery_effects=dict(data.get("discovery_effects", {})),
        )


@dataclass(slots=True)
class DeathFallback:
    """关键 NPC 死亡后的剧情续接约束，由 DM 负责自然呈现。"""

    guidance: str = ""  # 必须保住的故事方向与替代线索载体
    consequence: str = ""  # 后续叙述应持续体现的非数值后果
    stuck_hint: str = ""  # 当前拍卡关时替代依赖该 NPC 的提示

    @classmethod
    def from_dict(cls, data: dict) -> "DeathFallback":
        """从字典构造关键 NPC 死亡续接约束。"""
        return cls(
            guidance=str(data.get("guidance", "")).strip(),
            consequence=str(data.get("consequence", "")).strip(),
            stuck_hint=str(data.get("stuck_hint", "")).strip(),
        )


@dataclass(slots=True)
class NpcSpec:
    """重要 NPC / Boss 的册页：带目标与秘密，必要时附可转战斗的卡面。"""

    id: str  # NPC id
    name: str  # 名字
    role: str = ""  # 身份/定位
    goal: str = ""  # 目标（驱动 DM 即兴时的动机）
    secret: str = ""  # 秘密（仅 DM 可见，不可直接抖给玩家）
    disposition: str = "neutral"  # 态度：hostile | neutral | friendly
    card: dict | None = None  # 可选：转战斗时用的英文键卡面
    story_critical: bool = False  # 死亡时必须启用剧情续接约束
    death_fallback: DeathFallback | None = None  # 死亡后的方向、后果与卡关替代

    @classmethod
    def from_dict(cls, data: dict) -> "NpcSpec":
        """从字典构造 NPC 册页。"""
        fallback = data.get("death_fallback")
        return cls(
            id=data["id"],
            name=str(data.get("name", data["id"])),
            role=str(data.get("role", "")),
            goal=str(data.get("goal", "")),
            secret=str(data.get("secret", "")),
            disposition=str(data.get("disposition", "neutral")),
            card=data.get("card"),
            story_critical=bool(data.get("story_critical", False)),
            death_fallback=(
                DeathFallback.from_dict(fallback)
                if isinstance(fallback, dict)
                else None
            ),
        )


@dataclass(slots=True)
class LocationSpec:
    """一个主要地点。``intra_exits`` 是珠内地点互通（不跨拍）。"""

    id: str  # 地点 id
    name: str  # 地点名
    description: str = ""  # 环境描述
    intra_exits: list[str] = field(default_factory=list)  # 珠内可去的其它地点 id

    @classmethod
    def from_dict(cls, data: dict) -> "LocationSpec":
        """从字典构造地点。"""
        return cls(
            id=data["id"],
            name=str(data.get("name", data["id"])),
            description=str(data.get("description", "")),
            intra_exits=list(data.get("intra_exits", [])),
        )


@dataclass(slots=True)
class Encounter:
    """conflict/climax 拍预置的遭遇模板：战斗触发时把这些参数带给战斗子图。"""

    id: str  # 遭遇 id
    location_id: str | None = None  # 新 Canon 明确绑定遭遇发生地点
    monster_ids: list[str] = field(
        default_factory=list
    )  # 参战的敌方在场者 actor_id（卡面在 entry_state.actors 里）
    surprised: list[str] = field(default_factory=list)  # 被突袭者 id
    loot_table: list[Any] = field(
        default_factory=list
    )  # 胜利结算展示摘要（不写入背包）
    xp_reward: int = 0  # 玩家胜利时每位参战角色获得的完整经验
    random_seed: int | None = None  # 可复现随机源
    on_win_flags: list[str] = field(
        default_factory=list
    )  # 玩家胜利时引擎自动写入的 flag（须在白名单内）
    on_win_discoveries: list[str] = field(
        default_factory=list
    )  # 击败敌人后可无障碍取得的本拍线索 id

    @classmethod
    def from_dict(cls, data: dict) -> "Encounter":
        """从字典构造遭遇模板。"""
        seed = data.get("random_seed")
        return cls(
            id=data["id"],
            location_id=(
                str(data["location_id"])
                if data.get("location_id") is not None
                else None
            ),
            monster_ids=list(data.get("monster_ids", [])),
            surprised=list(data.get("surprised", [])),
            loot_table=list(data.get("loot_table", [])),
            xp_reward=max(0, int(data.get("xp_reward", 0))),
            random_seed=int(seed) if seed is not None else None,
            on_win_flags=list(data.get("on_win_flags", [])),
            on_win_discoveries=list(data.get("on_win_discoveries", [])),
        )


@dataclass(slots=True)
class Beat:
    """一拍 / 一颗糖葫芦珠：珠内自由沙盒 + 离开它的推进条件。"""

    id: str  # 拍 id
    title: str  # 拍标题
    kind: BeatKind  # 拍类型
    act_id: str = ""  # 所属幕；旧 Canon 缺省为空
    estimated_minutes: int = 0  # 本拍预计用时；旧 Canon 缺省为 0
    objective: str = ""  # 当前可执行目标
    pressure: str = ""  # 当前压力或升级来源
    relevant_clue_ids: list[str] = field(default_factory=list)  # 长局只回忆相关旧线索
    payoff_flag_ids: list[str] = field(default_factory=list)  # 本拍需回应的既有选择
    location_ids: list[str] = field(
        default_factory=list
    )  # 珠内沙盒地点（可多个，真沙盒）
    entry_state: dict = field(
        default_factory=dict
    )  # 进入这拍时的世界初始状态（搭 scene 用，见 build_beat_scene）
    key_info: list[KeyInfo] = field(default_factory=list)  # DM 必须传达的关键线索
    advance_conditions: list[Trigger] = field(default_factory=list)  # 推进条件
    exits: list[Exit] = field(default_factory=list)  # 出口（trigger_id → next_beat_id）
    stuck_fallback: dict = field(
        default_factory=dict
    )  # 卡关兜底：{hint, reveal_clue, point_to_exit}
    encounter: Encounter | None = None  # 可选：预置遭遇
    ending_outcome: EndingOutcome | None = None  # 仅 ending 拍：胜局 / 败局

    def exit_for(self, trigger_id: str) -> Exit | None:
        """取某触发器对应的出口。"""
        return next((e for e in self.exits if e.trigger_id == trigger_id), None)

    @property
    def is_ending(self) -> bool:
        """是否为结局拍。"""
        return self.kind == BeatKind.ENDING

    @classmethod
    def from_dict(cls, data: dict) -> "Beat":
        """从字典构造一拍。"""
        ending = data.get("ending_outcome")
        encounter = data.get("encounter")
        return cls(
            id=data["id"],
            title=str(data.get("title", data["id"])),
            kind=BeatKind(data["kind"]),
            act_id=str(data.get("act_id", "")),
            estimated_minutes=max(0, int(data.get("estimated_minutes", 0))),
            objective=str(data.get("objective", "")),
            pressure=str(data.get("pressure", "")),
            relevant_clue_ids=list(data.get("relevant_clue_ids", [])),
            payoff_flag_ids=list(data.get("payoff_flag_ids", [])),
            location_ids=list(data.get("location_ids", [])),
            entry_state=dict(data.get("entry_state", {})),
            key_info=[KeyInfo.from_dict(k) for k in data.get("key_info", [])],
            advance_conditions=[
                Trigger.from_dict(t) for t in data.get("advance_conditions", [])
            ],
            exits=[Exit.from_dict(e) for e in data.get("exits", [])],
            stuck_fallback=dict(data.get("stuck_fallback", {})),
            encounter=Encounter.from_dict(encounter) if encounter else None,
            ending_outcome=EndingOutcome(ending) if ending else None,
        )


# ---------------------------------------------------------------------------
# 顶层：剧情圣经
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Canon:
    """整局冻结的剧情圣经：大纲 + 珠子串 + 整局胜负条件。只读。"""

    campaign_id: str  # 本局 canon 的唯一 id（注册表 key）
    title: str  # 标题
    premise: str = ""  # 一句话主线
    theme: str = ""  # 主题
    tone: str = ""  # 基调
    duration_minutes: int = 20  # 预计单局时长（故事广场公开元数据）
    length_mode: str = "short"  # short | standard | long；旧 Canon 推导为 short
    act_count: int = 1  # 幕数量；旧 Canon 缺省为 1
    runtime_location_scoping: bool = (
        False  # 新生成 Canon 启用；旧 Canon 保持原有披露行为
    )
    recommended_player_count: int = 1  # 推荐玩家人数，仅展示、不限制开局
    gameplay_focus: list[str] = field(default_factory=list)  # 玩法侧重
    content_warnings: list[str] = field(default_factory=list)  # 玩家可见内容提示
    win_condition: Trigger | None = None  # 整局胜利条件
    lose_condition: Trigger | None = None  # 整局失败条件
    declared_flags: list[str] = field(
        default_factory=list
    )  # flag 白名单（DM 只能写这里声明过的）
    cast: list[NpcSpec] = field(default_factory=list)  # 重要 NPC / Boss 册
    locations: list[LocationSpec] = field(default_factory=list)  # 主要地点
    action_definitions: list[ActionDefinition] = field(
        default_factory=list
    )  # 物品与任务特性的统一规则行动
    beats: list[Beat] = field(default_factory=list)  # 主线珠子串
    start_beat_id: str = ""  # 起始拍 id

    # ---- 查找 ----
    def beat(self, beat_id: str) -> Beat | None:
        """按 id 取一拍。"""
        return next((b for b in self.beats if b.id == beat_id), None)

    def npc(self, npc_id: str) -> NpcSpec | None:
        """按 id 取一个 NPC 册页。"""
        return next((n for n in self.cast if n.id == npc_id), None)

    def encounter(self, encounter_id: str) -> tuple[Beat, Encounter] | None:
        """按 id 取预置遭遇及其所属剧情拍。"""
        for beat in self.beats:
            if beat.encounter is not None and beat.encounter.id == encounter_id:
                return beat, beat.encounter
        return None

    def clue(self, clue_id: str) -> tuple[Beat, KeyInfo] | None:
        """按 id 取关键线索及其所属剧情拍。"""
        for beat in self.beats:
            for clue in beat.key_info:
                if clue.id == clue_id:
                    return beat, clue
        return None

    def location(self, location_id: str) -> LocationSpec | None:
        """按 id 取一个地点。"""
        return next((loc for loc in self.locations if loc.id == location_id), None)

    def ending_beat(self, outcome: EndingOutcome) -> Beat | None:
        """取某归属（胜/败）的结局拍。"""
        return next(
            (b for b in self.beats if b.is_ending and b.ending_outcome == outcome), None
        )

    def action_definition(self, action_id: str) -> ActionDefinition | None:
        """按 id 取一个 Canon 规则行动定义。"""
        return next(
            (action for action in self.action_definitions if action.id == action_id),
            None,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Canon":
        """从（手写或生成的）JSON 字典构造剧情圣经。"""
        win = data.get("win_condition")
        lose = data.get("lose_condition")
        raw_beats = list(data.get("beats", []))
        duration = int(data.get("duration_minutes", 20))
        length_mode = str(data.get("length_mode", "short"))
        derived_acts = {
            str(beat.get("act_id")) for beat in raw_beats if beat.get("act_id")
        }
        return cls(
            campaign_id=str(data["campaign_id"]),
            title=str(data.get("title", data["campaign_id"])),
            premise=str(data.get("premise", "")),
            theme=str(data.get("theme", "")),
            tone=str(data.get("tone", "")),
            duration_minutes=duration,
            length_mode=length_mode,
            act_count=int(data.get("act_count", len(derived_acts) or 1)),
            runtime_location_scoping=bool(data.get("runtime_location_scoping", False)),
            recommended_player_count=int(data.get("recommended_player_count", 1)),
            gameplay_focus=[str(item) for item in data.get("gameplay_focus", [])],
            content_warnings=[str(item) for item in data.get("content_warnings", [])],
            win_condition=Trigger.from_dict(win) if win else None,
            lose_condition=Trigger.from_dict(lose) if lose else None,
            declared_flags=list(data.get("declared_flags", [])),
            cast=[NpcSpec.from_dict(n) for n in data.get("cast", [])],
            locations=[
                LocationSpec.from_dict(loc) for loc in data.get("locations", [])
            ],
            action_definitions=[
                ActionDefinition.from_dict(item)
                for item in data.get("action_definitions", [])
            ],
            beats=[Beat.from_dict(b) for b in raw_beats],
            start_beat_id=str(data.get("start_beat_id", "")),
        )


# ---------------------------------------------------------------------------
# 确定性触发判定（纯函数，不碰图状态）
# ---------------------------------------------------------------------------
def evaluate_trigger(
    trigger: Trigger,
    story: dict,
    scene: dict,
    party: dict[str, Combatant],
    last_combat: dict | None,
) -> bool | None:
    """判定一个触发器是否命中。

    返回 ``True``/``False`` 表示引擎能确定性判出结果；返回 ``None`` 表示
    ``semantic`` 触发器引擎判不了，需上层改问 DM 一道是/否题。

    各类判据（predicate 形状）：
    - flag：``{flag, equals?}`` 单个 flag 等于某值（equals 缺省 True）；
      或 ``{all: [...]}`` 全部为 True；或 ``{any: [...]}`` 任一为 True。
    - item：``{item_id}`` —— 队伍任一角色背包持有该道具且数量 > 0。
    - location：``{location_id}`` —— 玩家当前在该地点或曾到达过。
    - combat_outcome：``{outcome}`` —— 最近一场战斗的结果等于它（如 ``players_win``）。
    - semantic：返回 None（留给 DM）。
    """
    pred = trigger.predicate or {}
    if trigger.kind == TriggerKind.FLAG:
        flags = story.get("flags", {})
        if "all" in pred:
            return all(flags.get(f) is True for f in pred["all"])
        if "any" in pred:
            return any(flags.get(f) is True for f in pred["any"])
        return flags.get(pred.get("flag")) == pred.get("equals", True)
    if trigger.kind == TriggerKind.ITEM:
        item_id = pred.get("item_id")
        return any(
            getattr(item, "item_id", None) == item_id
            and getattr(item, "quantity", 0) > 0
            for c in party.values()
            for item in getattr(c, "inventory", [])
        )
    if trigger.kind == TriggerKind.LOCATION:
        location_id = pred.get("location_id")
        return story.get(
            "current_location_id"
        ) == location_id or location_id in story.get("visited_locations", [])
    if trigger.kind == TriggerKind.COMBAT_OUTCOME:
        combat = last_combat or {}
        if combat.get("outcome") != pred.get("outcome"):
            return False
        encounter_id = pred.get("encounter_id")
        return encounter_id is None or combat.get("encounter_id") == encounter_id
    if trigger.kind == TriggerKind.ACTION:
        # action 出口只接受已规范化的显式 transition 写入，不能靠回合末自动猜中。
        return False
    # semantic：引擎判不了
    return None


# ---------------------------------------------------------------------------
# 喂给 DM 的当前拍骨架（让叙述「长在骨架上」，§4.2）
# ---------------------------------------------------------------------------
def managed_flag_sources(canon: Canon) -> dict[str, list[dict[str, str]]]:
    """汇总由确定性引擎管理的 flag 及其原子写入来源。

    discovery flag 只能在玩家真正发现线索时写入；遭遇胜利 flag 只能在对应战斗
    结算时写入。未出现在结果中的普通 flag 才允许 DM 通过 ``flags_set`` 声明。
    """
    sources: dict[str, list[dict[str, str]]] = {}
    for beat in canon.beats:
        for flag in beat.entry_state.get("flags") or {}:
            sources.setdefault(str(flag), []).append(
                {"kind": "initial_state", "beat_id": beat.id, "owner_id": beat.id}
            )
        for clue in beat.key_info:
            for flag in clue.discovery_effects.get("flags_set") or {}:
                sources.setdefault(str(flag), []).append(
                    {
                        "kind": "discovery",
                        "beat_id": beat.id,
                        "clue_id": clue.id,
                        "owner_id": clue.id,
                    }
                )
        if beat.encounter is None:
            continue
        for flag in beat.encounter.on_win_flags:
            sources.setdefault(str(flag), []).append(
                {
                    "kind": "encounter_win",
                    "beat_id": beat.id,
                    "encounter_id": beat.encounter.id,
                    "owner_id": beat.encounter.id,
                }
            )
    for action in canon.action_definitions:
        for effect in action.contract.get("effect_templates", []):
            if effect.get("kind") != "set_flag" or not effect.get("flag"):
                continue
            flag = str(effect["flag"])
            sources.setdefault(flag, []).append(
                {"kind": "rule_action", "action_id": action.id, "owner_id": action.id}
            )
    return sources


def beat_brief(canon: Canon, story: dict) -> dict | None:
    """把当前拍骨架压成 DM 画像：目标、线索、在场 NPC、死亡续接与出口提示。

    返回 None 表示当前拍找不到（异常局面，调用方回落到无骨架叙述）。
    """
    beat = canon.beat(story.get("current_beat_id", ""))
    if beat is None:
        return None

    delivered = set(story.get("delivered_clues", []))
    discovered_ids = list(story.get("discovered_clues", []))
    discovered = set(discovered_ids)
    current_location_id = story.get("current_location_id")
    location_scoped = canon.runtime_location_scoping
    local_clues = [
        clue
        for clue in beat.key_info
        if not location_scoped
        or clue.location_id is None
        or clue.location_id == current_location_id
    ]
    on_win_discoveries = set(
        beat.encounter.on_win_discoveries
        if beat.encounter is not None
        and (
            not location_scoped
            or beat.encounter.location_id is None
            or beat.encounter.location_id == current_location_id
        )
        else []
    )
    undelivered = [
        k.text
        for k in local_clues
        if k.id not in delivered and k.id not in on_win_discoveries
    ]
    available_discoveries = [
        {
            "id": clue.id,
            "text": clue.text,
            "discovery_effects": dict(clue.discovery_effects or {}),
        }
        for clue in local_clues
        if clue.id not in discovered and clue.id not in on_win_discoveries
    ]
    known_clues = []
    relevant_clue_ids = set(beat.relevant_clue_ids)
    for clue_id in discovered_ids:
        if relevant_clue_ids and clue_id not in relevant_clue_ids:
            continue
        resolved = canon.clue(clue_id)
        if resolved is None:
            continue
        _, clue = resolved
        known_clues.append({"id": clue.id, "text": clue.text})
    removed = set(story.get("removed_actor_ids", []))

    # 在场 NPC：entry_state.actors 里能在 cast 中找到册页的，连同目标/秘密一并给 DM（仅供把控方向）
    on_stage = []
    for actor in beat.entry_state.get("actors", []):
        actor_id = actor.get("actor_id") or actor.get("npc_ref", "")
        actor_location_id = actor.get(
            "location_id", beat.entry_state.get("location_id")
        )
        if location_scoped and actor_location_id != current_location_id:
            continue
        if actor_id in removed:
            continue
        spec = canon.npc(actor_id)
        if spec is not None:
            on_stage.append(
                {
                    "actor_id": spec.id,
                    "name": spec.name,
                    "role": spec.role,
                    "goal": spec.goal,
                    "secret": spec.secret,
                }
            )

    critical_deaths = []
    for actor_id in story.get("critical_npc_deaths", []):
        spec = canon.npc(actor_id)
        if spec is None or not spec.story_critical or spec.death_fallback is None:
            continue
        critical_deaths.append(
            {
                "actor_id": spec.id,
                "name": spec.name,
                "role": spec.role,
                "guidance": spec.death_fallback.guidance,
                "consequence": spec.death_fallback.consequence,
            }
        )

    visible_location_ids = set(beat.location_ids)
    if location_scoped and current_location_id:
        current_location = canon.location(current_location_id)
        visible_location_ids = {
            current_location_id,
            *(current_location.intra_exits if current_location is not None else []),
        }
    locations = [
        {"id": loc.id, "name": loc.name, "description": loc.description}
        for lid in beat.location_ids
        if lid in visible_location_ids
        if (loc := canon.location(lid)) is not None
    ]
    return {
        # 整局世界锚点：让叙述始终对得上既定设定（如地名/地理关系），别凭空发挥
        "premise": canon.premise,
        "theme": canon.theme,
        "tone": canon.tone,
        "beat_title": beat.title,
        "beat_id": beat.id,
        "beat_kind": str(beat.kind.value),
        "act_id": beat.act_id,
        "objective": beat.objective,
        "pressure": beat.pressure,
        "act_recap": str(story.get("act_recap", "")),
        "payoff_flags": {
            flag_id: (story.get("flags") or {}).get(flag_id)
            for flag_id in beat.payoff_flag_ids
        },
        "locations": locations,
        "undelivered_clues": undelivered,
        "available_discoveries": available_discoveries,
        "known_clues": known_clues,
        "current_flags": dict(story.get("flags", {})),
        "delivered_clue_ids": sorted(delivered),
        "discovered_clue_ids": sorted(discovered),
        "npcs": on_stage,
        "critical_npc_deaths": critical_deaths,
        "advance_hints": [
            t.description for t in beat.advance_conditions if t.description
        ],
        "allowed_flags": list(canon.declared_flags),
        "allowed_delivery_clue_ids": [
            clue.id
            for clue in local_clues
            if clue.id not in delivered and clue.id not in on_win_discoveries
        ],
        "allowed_discovery_clue_ids": [
            clue.id
            for clue in local_clues
            if clue.id not in discovered and clue.id not in on_win_discoveries
        ],
        "managed_flag_sources": managed_flag_sources(canon),
        "reachable_transitions": [
            {
                "trigger_id": ex.trigger_id,
                "trigger_kind": (
                    trigger.kind.value
                    if (
                        trigger := next(
                            (
                                item
                                for item in beat.advance_conditions
                                if item.id == ex.trigger_id
                            ),
                            None,
                        )
                    )
                    else None
                ),
                "to_beat_id": ex.next_beat_id,
            }
            for ex in beat.exits
        ],
        # 新 Canon 只披露当前地点；旧 Canon 保留原先的相邻拍遭遇提示。
        "reachable_encounters": (
            []
            if location_scoped
            else [
                {
                    "encounter_id": next_beat.encounter.id,
                    "beat_id": next_beat.id,
                    "monster_ids": list(next_beat.encounter.monster_ids),
                }
                for exit_ in beat.exits
                if (next_beat := canon.beat(exit_.next_beat_id)) is not None
                and next_beat.encounter is not None
            ]
        ),
        "current_encounter": (
            {
                "encounter_id": beat.encounter.id,
                "beat_id": beat.id,
                "monster_ids": list(beat.encounter.monster_ids),
            }
            if beat.encounter is not None
            and (
                not location_scoped
                or beat.encounter.location_id is None
                or beat.encounter.location_id == current_location_id
            )
            else None
        ),
    }


# ---------------------------------------------------------------------------
# 结构性校验：把「一定能结束」从祈祷变成编译期断言（§五.2）
# ---------------------------------------------------------------------------
def validate_canon(canon: Canon) -> list[str]:
    """校验剧情圣经的结构闭合，返回错误信息列表（空列表表示通过）。

    校验项：起始拍存在；每拍从起始拍可达；非结局拍至少一个出口；存在可达的结局拍；
    出口 / 地点引用无悬空；关键 NPC 有死亡续接；flag 类触发器与战斗胜利写入的 flag
    都在白名单内；胜负条件已定义。
    """
    errors: list[str] = []

    duration_limits = {
        "short": (10, 30),
        "standard": (31, 60),
        "long": (61, 120),
    }
    if canon.length_mode not in duration_limits:
        errors.append("length_mode 必须是 short、standard 或 long")
    elif not canon.runtime_location_scoping:
        # 旧 Canon 缺省为 short，但其历史时长元数据不受新分档约束。
        if not 10 <= canon.duration_minutes <= 120:
            errors.append("旧 Canon 的 duration_minutes 必须在 10 到 120 之间")
    else:
        lower, upper = duration_limits[canon.length_mode]
        if not lower <= canon.duration_minutes <= upper:
            errors.append(
                f"{canon.length_mode} 的 duration_minutes 必须在 {lower} 到 {upper} 之间"
            )
    if canon.act_count < 1:
        errors.append("act_count 必须是正整数")
    if not 1 <= canon.recommended_player_count <= 6:
        errors.append("recommended_player_count 必须在 1 到 6 之间")
    if not canon.gameplay_focus:
        errors.append("gameplay_focus 至少需要一项")

    _validate_ids("campaign", [canon.campaign_id], errors)
    _validate_ids("beat", [beat.id for beat in canon.beats], errors)
    _validate_ids("location", [location.id for location in canon.locations], errors)
    _validate_ids("actor", [npc.id for npc in canon.cast], errors)
    _validate_ids("flag", canon.declared_flags, errors)
    _validate_ids("action", [action.id for action in canon.action_definitions], errors)
    trigger_ids_all = [
        trigger.id for beat in canon.beats for trigger in beat.advance_conditions
    ]
    trigger_ids_all.extend(
        condition.id
        for condition in (canon.win_condition, canon.lose_condition)
        if condition is not None
    )
    _validate_ids("trigger", trigger_ids_all, errors)
    _validate_ids(
        "clue",
        [clue.id for beat in canon.beats for clue in beat.key_info],
        errors,
    )
    beat_ids = {b.id for b in canon.beats}
    location_ids = {loc.id for loc in canon.locations}
    declared = set(canon.declared_flags)
    encounter_ids: set[str] = set()

    for npc in canon.cast:
        if npc.card is not None and npc.card.get("id") != npc.id:
            errors.append(f"NPC «{npc.id}» 的 card.id 必须与 actor id 相同")
        if not npc.story_critical:
            continue
        if npc.death_fallback is None:
            errors.append(f"关键 NPC «{npc.id}» 缺少 death_fallback")
            continue
        if not npc.death_fallback.guidance:
            errors.append(f"关键 NPC «{npc.id}» 的 death_fallback.guidance 不能为空")
        if not npc.death_fallback.consequence:
            errors.append(f"关键 NPC «{npc.id}» 的 death_fallback.consequence 不能为空")

    # 起始拍
    if not canon.start_beat_id:
        errors.append("缺少 start_beat_id")
    elif canon.start_beat_id not in beat_ids:
        errors.append(f"start_beat_id «{canon.start_beat_id}» 不存在于 beats")

    # 胜负条件
    if canon.win_condition is None:
        errors.append("缺少 win_condition")
    if canon.lose_condition is None:
        errors.append("缺少 lose_condition")

    # 结局拍存在性（胜/败各一）
    if canon.ending_beat(EndingOutcome.WIN) is None:
        errors.append("缺少 ending_outcome=win 的结局拍")
    if canon.ending_beat(EndingOutcome.LOSE) is None:
        errors.append("缺少 ending_outcome=lose 的结局拍")

    # 出口 / 触发器 / 地点 / flag 的逐拍校验
    for location in canon.locations:
        for destination_id in location.intra_exits:
            if destination_id not in location_ids:
                errors.append(
                    f"地点 «{location.id}» 的 intra_exits 引用了不存在的地点 «{destination_id}»"
                )

    for beat in canon.beats:
        trigger_ids = {t.id for t in beat.advance_conditions}
        for ex in beat.exits:
            if ex.next_beat_id not in beat_ids:
                errors.append(
                    f"拍 «{beat.id}» 的出口指向不存在的 next_beat_id «{ex.next_beat_id}»"
                )
            if ex.trigger_id not in trigger_ids:
                errors.append(
                    f"拍 «{beat.id}» 的出口引用了不存在的 trigger_id «{ex.trigger_id}»"
                )
        for lid in beat.location_ids:
            if lid not in location_ids:
                errors.append(f"拍 «{beat.id}» 引用了不存在的 location_id «{lid}»")
        for clue in beat.key_info:
            if (
                clue.location_id is not None
                and clue.location_id not in beat.location_ids
            ):
                errors.append(
                    f"拍 «{beat.id}» 线索 «{clue.id}» 的 location_id 不在本拍地点中"
                )
        entry_location_id = beat.entry_state.get("location_id")
        preserve_current_scene = beat.entry_state.get("preserve_current_scene") is True
        if entry_location_id is not None and entry_location_id not in location_ids:
            errors.append(
                f"拍 «{beat.id}» 的 entry_state 引用了不存在的 location_id «{entry_location_id}»"
            )
        if entry_location_id is not None and entry_location_id not in beat.location_ids:
            errors.append(
                f"拍 «{beat.id}» 的 entry_state.location_id 不在本拍 location_ids 中"
            )
        if entry_location_id is None and not (
            beat.is_ending and preserve_current_scene
        ):
            errors.append(f"拍 «{beat.id}» 缺少 entry_state.location_id")
        for actor in beat.entry_state.get("actors", []):
            actor_id = actor.get("actor_id") or actor.get("npc_ref")
            actor_location_id = actor.get("location_id")
            if actor_id and canon.npc(actor_id) is None:
                errors.append(
                    f"拍 «{beat.id}» 的 entry_state 引用了不存在的 actor «{actor_id}»"
                )
            if actor_location_id not in beat.location_ids:
                errors.append(
                    f"拍 «{beat.id}» 的 actor «{actor_id}» 位于本拍之外的地点 «{actor_location_id}»"
                )
        for t in beat.advance_conditions:
            if t.kind == TriggerKind.FLAG:
                referenced = _flag_trigger_names(t)
                for flag in referenced:
                    if flag not in declared:
                        errors.append(
                            f"拍 «{beat.id}» 触发器 «{t.id}» 的 flag «{flag}» 不在 declared_flags 白名单内"
                        )
        if beat.encounter is not None:
            if (
                beat.encounter.location_id is not None
                and beat.encounter.location_id not in beat.location_ids
            ):
                errors.append(
                    f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 的 location_id 不在本拍地点中"
                )
            if beat.encounter.id in encounter_ids:
                errors.append(f"遭遇 id «{beat.encounter.id}» 重复")
            encounter_ids.add(beat.encounter.id)
            for flag in beat.encounter.on_win_flags:
                if flag not in declared:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇胜利写入的 flag «{flag}» 不在 declared_flags 白名单内"
                    )
            current_clue_ids = {clue.id for clue in beat.key_info}
            seen_on_win_discoveries: set[str] = set()
            for clue_id in beat.encounter.on_win_discoveries:
                if clue_id in seen_on_win_discoveries:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 的 "
                        f"on_win_discoveries 重复引用线索 «{clue_id}»"
                    )
                    continue
                seen_on_win_discoveries.add(clue_id)
                resolved = canon.clue(clue_id)
                if resolved is None:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 的 "
                        f"on_win_discoveries 引用了不存在的线索 «{clue_id}»"
                    )
                elif clue_id not in current_clue_ids:
                    owner_beat, _ = resolved
                    errors.append(
                        f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 的 "
                        f"on_win_discoveries 线索 «{clue_id}» 属于另一拍 «{owner_beat.id}»"
                    )
            actor_entries = {
                actor.get("actor_id") or actor.get("npc_ref"): actor
                for actor in beat.entry_state.get("actors", [])
            }
            for monster_id in beat.encounter.monster_ids:
                actor = actor_entries.get(monster_id)
                spec = canon.npc(monster_id)
                card = (actor or {}).get("card") or (spec.card if spec else None)
                if actor is None and spec is None:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 引用了不存在的 actor «{monster_id}»"
                    )
                elif not card:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇 «{beat.encounter.id}» 的 actor «{monster_id}» 缺少战斗卡面"
                    )
        for clue in beat.key_info:
            effects = clue.discovery_effects or {}
            for flag in effects.get("flags_set") or {}:
                if flag not in declared:
                    errors.append(
                        f"拍 «{beat.id}» 线索 «{clue.id}» 的发现效果 flag «{flag}» 不在白名单内"
                    )
        # 非结局拍必须有出路
        if not beat.is_ending and not beat.exits:
            errors.append(f"非结局拍 «{beat.id}» 没有任何出口（会卡死）")

    for condition_name, condition in (
        ("win_condition", canon.win_condition),
        ("lose_condition", canon.lose_condition),
    ):
        encounter_id = (
            condition.predicate.get("encounter_id")
            if condition is not None and condition.kind == TriggerKind.COMBAT_OUTCOME
            else None
        )
        if encounter_id and encounter_id not in encounter_ids:
            errors.append(f"{condition_name} 引用了不存在的遭遇 «{encounter_id}»")

    for action in canon.action_definitions:
        requirements = action.requirements
        for flag in requirements.get("flags", []):
            if flag not in declared:
                errors.append(f"规则行动 «{action.id}» 引用了未声明 flag «{flag}»")
        for beat_id in requirements.get("beat_ids", []):
            if beat_id not in beat_ids:
                errors.append(f"规则行动 «{action.id}» 引用了不存在的 beat «{beat_id}»")
        for location_id in requirements.get("location_ids", []):
            if location_id not in location_ids:
                errors.append(
                    f"规则行动 «{action.id}» 引用了不存在的 location «{location_id}»"
                )
        for encounter_id in requirements.get("encounter_ids", []):
            if encounter_id not in encounter_ids:
                errors.append(
                    f"规则行动 «{action.id}» 引用了不存在的 encounter «{encounter_id}»"
                )
        for effect in action.contract.get("effect_templates", []):
            if effect.get("kind") == "set_flag" and effect.get("flag") not in declared:
                errors.append(
                    f"规则行动 «{action.id}» 写入了未声明 flag «{effect.get('flag')}»"
                )
            if (
                effect.get("kind") == "move_location"
                and effect.get("location_id") not in location_ids
            ):
                errors.append(
                    f"规则行动 «{action.id}» 移动到不存在的 location «{effect.get('location_id')}»"
                )
            if (
                effect.get("kind") == "transition_beat"
                and effect.get("beat_id") not in beat_ids
            ):
                errors.append(
                    f"规则行动 «{action.id}» 迁移到不存在的 beat «{effect.get('beat_id')}»"
                )
            if effect.get("kind") == "transition_beat":
                source_beat_ids = requirements.get("beat_ids", [])
                if not source_beat_ids:
                    errors.append(
                        f"跨拍规则行动 «{action.id}» 必须用 requirements.beat_ids 限定来源拍"
                    )
                for source_beat_id in source_beat_ids:
                    source_beat = canon.beat(source_beat_id)
                    if source_beat is None:
                        continue
                    action_trigger_ids = {
                        trigger.id
                        for trigger in source_beat.advance_conditions
                        if trigger.kind == TriggerKind.ACTION
                    }
                    legal_targets = {
                        exit_.next_beat_id
                        for exit_ in source_beat.exits
                        if exit_.trigger_id in action_trigger_ids
                    }
                    if effect.get("beat_id") not in legal_targets:
                        errors.append(
                            f"规则行动 «{action.id}» 从拍 «{source_beat_id}» "
                            f"不能迁移到非 action 出口 «{effect.get('beat_id')}»"
                        )

    # 可达性：从起始拍沿出口 BFS；结局拍可由引擎按胜负条件直接跳入，故视为可达根
    reachable = _reachable_beats(canon)
    for beat in canon.beats:
        if beat.id not in reachable:
            errors.append(f"拍 «{beat.id}» 从 start_beat_id 不可达（孤岛）")

    return errors


def validate_authored_canon(canon: Canon) -> list[str]:
    """严格校验新生成 Canon 的持久化效果是否只有一个原子写入入口。

    该校验服务于故事生成、修复与发布；已有磁盘 Canon 仍只走
    :func:`validate_canon`，避免历史玩家副本因新增创作规则而无法加载。
    """
    errors: list[str] = []
    for flag, sources in managed_flag_sources(canon).items():
        if len(sources) <= 1:
            continue
        owner_labels = {
            "discovery": "线索",
            "encounter_win": "遭遇",
            "initial_state": "初始状态",
            "rule_action": "规则行动",
        }
        owners = [
            f"{owner_labels.get(source['kind'], source['kind'])} {source.get('owner_id', '')}"
            for source in sources
        ]
        errors.append(f"flag «{flag}» 存在多个原子写入入口：{'、'.join(owners)}")

    item_sources: dict[str, list[str]] = {}
    for beat in canon.beats:
        for clue in beat.key_info:
            for grant in clue.discovery_effects.get("grant_items", []) or []:
                item_id = str(grant.get("item_id") or "")
                if not item_id:
                    continue
                item_sources.setdefault(item_id, []).append(f"线索 {beat.id}/{clue.id}")
        if beat.encounter is None:
            continue
        for loot in beat.encounter.loot_table:
            if isinstance(loot, dict) and loot.get("item_id"):
                errors.append(
                    f"遭遇 «{beat.encounter.id}» 的 loot_table 不能作为持久化物品 "
                    f"«{loot['item_id']}» 的入库入口；请改用唯一线索的 grant_items"
                )

    for item_id, owners in item_sources.items():
        if len(owners) > 1:
            errors.append(f"物品 «{item_id}» 存在多个原子发放入口：{'、'.join(owners)}")
    return errors


def _validate_ids(kind: str, values: list[Any], errors: list[str]) -> None:
    """校验同类 Canon ID 非空、snake_case 且全局唯一。"""
    seen: set[str] = set()
    for raw_value in values:
        if not isinstance(raw_value, str) or not _CANON_ID_PATTERN.fullmatch(raw_value):
            errors.append(f"{kind} id «{raw_value}» 必须是 lowercase snake_case")
            continue
        if raw_value in seen:
            errors.append(f"{kind} id «{raw_value}» 重复")
        seen.add(raw_value)


def _flag_trigger_names(trigger: Trigger) -> list[str]:
    """取一个 flag 触发器引用到的所有 flag 名（兼容单个 flag 与 all/any 列表）。"""
    pred = trigger.predicate or {}
    if "all" in pred:
        return list(pred["all"])
    if "any" in pred:
        return list(pred["any"])
    flag = pred.get("flag")
    return [flag] if flag is not None else []


def _reachable_beats(canon: Canon) -> set[str]:
    """从起始拍沿出口做 BFS，求可达拍集合；两个结局拍作为引擎可直接跳入的根一并纳入。"""
    seeds = [canon.start_beat_id]
    for outcome in (EndingOutcome.WIN, EndingOutcome.LOSE):
        ending = canon.ending_beat(outcome)
        if ending is not None:
            seeds.append(ending.id)

    reachable: set[str] = set()
    queue: deque[str] = deque(s for s in seeds if s)
    while queue:
        bid = queue.popleft()
        if bid in reachable:
            continue
        reachable.add(bid)
        beat = canon.beat(bid)
        if beat is None:
            continue
        for ex in beat.exits:
            if ex.next_beat_id not in reachable:
                queue.append(ex.next_beat_id)
    return reachable
