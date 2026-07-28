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
from typing import Any

from src.model.combatant import Combatant
from src.model.enums import Ability, ConditionType, StrEnum


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
    discovery_effects: dict[str, Any] = field(
        default_factory=dict
    )  # 玩家真正发现后由引擎提交的 flag / 物品效果

    @classmethod
    def from_dict(cls, data: dict) -> "KeyInfo":
        """从字典构造关键线索。"""
        return cls(
            id=data["id"],
            text=str(data.get("text", "")),
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
    monster_ids: list[str] = field(
        default_factory=list
    )  # 参战的敌方在场者 actor_id（卡面在 entry_state.actors 里）
    surprised: list[str] = field(default_factory=list)  # 被突袭者 id
    loot_table: list[Any] = field(default_factory=list)  # 战利品表（玩家胜利时发放）
    xp_reward: int = 0  # 玩家胜利时每位参战角色获得的完整经验
    random_seed: int | None = None  # 可复现随机源
    on_win_flags: list[str] = field(
        default_factory=list
    )  # 玩家胜利时引擎自动写入的 flag（须在白名单内）
    special_actions: list[dict[str, Any]] = field(
        default_factory=list
    )  # canon 封闭定义的特殊战斗行动（DM 只负责映射意图）

    @classmethod
    def from_dict(cls, data: dict) -> "Encounter":
        """从字典构造遭遇模板。"""
        seed = data.get("random_seed")
        return cls(
            id=data["id"],
            monster_ids=list(data.get("monster_ids", [])),
            surprised=list(data.get("surprised", [])),
            loot_table=list(data.get("loot_table", [])),
            xp_reward=max(0, int(data.get("xp_reward", 0))),
            random_seed=int(seed) if seed is not None else None,
            on_win_flags=list(data.get("on_win_flags", [])),
            special_actions=[
                dict(action) for action in data.get("special_actions", [])
            ],
        )


@dataclass(slots=True)
class Beat:
    """一拍 / 一颗糖葫芦珠：珠内自由沙盒 + 离开它的推进条件。"""

    id: str  # 拍 id
    title: str  # 拍标题
    kind: BeatKind  # 拍类型
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
    win_condition: Trigger | None = None  # 整局胜利条件
    lose_condition: Trigger | None = None  # 整局失败条件
    declared_flags: list[str] = field(
        default_factory=list
    )  # flag 白名单（DM 只能写这里声明过的）
    cast: list[NpcSpec] = field(default_factory=list)  # 重要 NPC / Boss 册
    locations: list[LocationSpec] = field(default_factory=list)  # 主要地点
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

    def location(self, location_id: str) -> LocationSpec | None:
        """按 id 取一个地点。"""
        return next((loc for loc in self.locations if loc.id == location_id), None)

    def ending_beat(self, outcome: EndingOutcome) -> Beat | None:
        """取某归属（胜/败）的结局拍。"""
        return next(
            (b for b in self.beats if b.is_ending and b.ending_outcome == outcome), None
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Canon":
        """从（手写或生成的）JSON 字典构造剧情圣经。"""
        win = data.get("win_condition")
        lose = data.get("lose_condition")
        return cls(
            campaign_id=data["campaign_id"],
            title=str(data.get("title", data["campaign_id"])),
            premise=str(data.get("premise", "")),
            theme=str(data.get("theme", "")),
            tone=str(data.get("tone", "")),
            win_condition=Trigger.from_dict(win) if win else None,
            lose_condition=Trigger.from_dict(lose) if lose else None,
            declared_flags=list(data.get("declared_flags", [])),
            cast=[NpcSpec.from_dict(n) for n in data.get("cast", [])],
            locations=[
                LocationSpec.from_dict(loc) for loc in data.get("locations", [])
            ],
            beats=[Beat.from_dict(b) for b in data.get("beats", [])],
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
def beat_brief(canon: Canon, story: dict) -> dict | None:
    """把当前拍骨架压成 DM 画像：目标、线索、在场 NPC、死亡续接与出口提示。

    返回 None 表示当前拍找不到（异常局面，调用方回落到无骨架叙述）。
    """
    beat = canon.beat(story.get("current_beat_id", ""))
    if beat is None:
        return None

    delivered = set(story.get("delivered_clues", []))
    undelivered = [k.text for k in beat.key_info if k.id not in delivered]
    discovered = set(story.get("discovered_clues", []))
    remaining_clues = [
        {"id": clue.id, "text": clue.text}
        for clue in beat.key_info
        if (
            clue.id not in discovered
            and (clue.discovery_effects or clue.id not in delivered)
        )
    ]
    removed = set(story.get("removed_actor_ids", []))

    # 在场 NPC：entry_state.actors 里能在 cast 中找到册页的，连同目标/秘密一并给 DM（仅供把控方向）
    on_stage = []
    for actor in beat.entry_state.get("actors", []):
        actor_id = actor.get("actor_id") or actor.get("npc_ref", "")
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

    locations = [
        {"id": loc.id, "name": loc.name, "description": loc.description}
        for lid in beat.location_ids
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
        "locations": locations,
        "undelivered_clues": undelivered,
        "remaining_key_clues": remaining_clues,
        "npcs": on_stage,
        "critical_npc_deaths": critical_deaths,
        "advance_hints": [
            t.description for t in beat.advance_conditions if t.description
        ],
        "allowed_flags": list(canon.declared_flags),
        "allowed_clue_ids": [k.id for k in beat.key_info],
        "discovery_managed_flags": [
            flag
            for candidate_beat in canon.beats
            for clue in candidate_beat.key_info
            for flag in (clue.discovery_effects.get("flags_set") or {})
        ],
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
        "reachable_encounters": [
            {
                "encounter_id": next_beat.encounter.id,
                "beat_id": next_beat.id,
                "monster_ids": list(next_beat.encounter.monster_ids),
            }
            for ex in beat.exits
            if (next_beat := canon.beat(ex.next_beat_id)) is not None
            and next_beat.encounter is not None
        ],
        "current_encounter": (
            {
                "encounter_id": beat.encounter.id,
                "beat_id": beat.id,
                "monster_ids": list(beat.encounter.monster_ids),
            }
            if beat.encounter is not None
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
    beat_ids = {b.id for b in canon.beats}
    location_ids = {loc.id for loc in canon.locations}
    declared = set(canon.declared_flags)
    encounter_ids: set[str] = set()

    for npc in canon.cast:
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
        for t in beat.advance_conditions:
            if t.kind == TriggerKind.FLAG:
                referenced = _flag_trigger_names(t)
                for flag in referenced:
                    if flag not in declared:
                        errors.append(
                            f"拍 «{beat.id}» 触发器 «{t.id}» 的 flag «{flag}» 不在 declared_flags 白名单内"
                        )
        if beat.encounter is not None:
            if beat.encounter.id in encounter_ids:
                errors.append(f"遭遇 id «{beat.encounter.id}» 重复")
            encounter_ids.add(beat.encounter.id)
            for flag in beat.encounter.on_win_flags:
                if flag not in declared:
                    errors.append(
                        f"拍 «{beat.id}» 遭遇胜利写入的 flag «{flag}» 不在 declared_flags 白名单内"
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
            _validate_special_actions(canon, beat, errors)
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

    # 可达性：从起始拍沿出口 BFS；结局拍可由引擎按胜负条件直接跳入，故视为可达根
    reachable = _reachable_beats(canon)
    for beat in canon.beats:
        if beat.id not in reachable:
            errors.append(f"拍 «{beat.id}» 从 start_beat_id 不可达（孤岛）")

    return errors


def _validate_special_actions(canon: Canon, beat: Beat, errors: list[str]) -> None:
    """校验遭遇特殊行动只引用封闭、可结算的规则原语。"""
    encounter = beat.encounter
    if encounter is None:
        return
    valid_effects = {"modify_ac", "modify_attack_bonus", "add_condition"}
    valid_ranges = {"melee", "any"}
    valid_abilities = {ability.value for ability in Ability}
    valid_conditions = {condition.value for condition in ConditionType}
    seen: set[str] = set()
    for action in encounter.special_actions:
        action_id = str(action.get("id") or "")
        if not action_id:
            errors.append(f"遭遇 «{encounter.id}» 存在缺少 id 的特殊行动")
            continue
        if action_id in seen:
            errors.append(f"遭遇 «{encounter.id}» 的特殊行动 id «{action_id}» 重复")
        seen.add(action_id)
        target_id = action.get("target_actor_id")
        if target_id not in encounter.monster_ids:
            errors.append(
                f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» 目标 «{target_id}» 不在参战者中"
            )
        if action.get("range", "any") not in valid_ranges:
            errors.append(f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» range 非法")
        for flag in action.get("requires_flags", []):
            if flag not in canon.declared_flags:
                errors.append(
                    f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» 引用了未声明 flag «{flag}»"
                )
        check = action.get("check")
        if check:
            if check.get("ability") not in valid_abilities:
                errors.append(
                    f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» ability 非法"
                )
            try:
                dc = int(check.get("dc"))
            except (TypeError, ValueError):
                dc = 0
            if not 1 <= dc <= 30:
                errors.append(f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» DC 非法")
        effect = action.get("effect") or {}
        if effect.get("kind") not in valid_effects:
            errors.append(
                f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» effect kind 非法"
            )
        if (
            effect.get("kind") == "add_condition"
            and effect.get("condition") not in valid_conditions
        ):
            errors.append(
                f"遭遇 «{encounter.id}» 的特殊行动 «{action_id}» condition 非法"
            )


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
