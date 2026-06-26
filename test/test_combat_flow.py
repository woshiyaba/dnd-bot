"""战斗全流程冒烟测试（可直接运行的驱动脚本）。

把 `CombatEngine` 当真实战斗一样从头跑到尾，演示并验证整条流程：

    enter_combat → judge_surprise → roll_initiative → next_turn
    → declare_action → resolve_action → narrate → check_end ... → settle

驱动方式（对应 docs/战斗/03-中断交互协议.md 第 5 节）：

    start_combat → {status: interrupted, interrupt: 请求}  # 跑到第一个玩家中断点
    submit(resume)   → 下一个中断点 / {status: finished}    # 玩家报骰/选择后恢复

本脚本用一个「自动玩家」(`auto_respond`) 替前端回报骰子与行动，并打印一条可读的
全程轨迹（先攻、每回合声明、命中/伤害、DM 叙述、最终结算）。

运行方式（仓库根目录）::

    uv run python test/test_combat_flow.py        # 直接看全程轨迹
    uv run python -m pytest test/test_combat_flow.py   # 当冒烟用例跑（无需 pytest-asyncio）

战斗子图自带 MemorySaver，无需 MySQL / DashScope，可独立运行。
DM 默认走确定性占位（启发式），所以同一 `random_seed` 下结果可复现。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 控制台默认 GBK，叙述里的中文/符号需用 UTF-8 输出，否则 print 直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # 某些环境的 stdout 不支持 reconfigure，忽略即可
    pass

# 允许「直接运行该文件」时也能 import 到 src（把仓库根目录加进 sys.path）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.combat.engine import CombatEngine  # noqa: E402
from src.model.enums import InterruptType  # noqa: E402


# ---------------------------------------------------------------------------
# 场景：1 名玩家 + 1 名 NPC 盟友  vs  2 只哥布林
# ---------------------------------------------------------------------------
def build_scene() -> dict:
    """构造一份用于全流程测试的场景上下文（卡面均为英文键）。

    - `random_seed` 固定 → 怪物/环境骰子可复现，整场回放一致。
    - `surprised` 把 goblin_2 列为被突袭，用来走「首轮跳过」分支。
    - 玩家攻防偏强、哥布林偏弱，保证这局能在数回合内分出胜负、跑到 settle。
    """
    return {
        "random_seed": 20260626,  # 固定随机种子，结果可复现
        "dm_mode": "llm",
        "surprised": ["goblin_2"],  # 被突袭名单：测「首轮跳过」分支
        "loot_table": [  # 胜利战利品：在 settle 节点发放
            {"item_id": "item_gold", "quantity": 50},
            {"item_id": "item_healing_potion", "quantity": 1},
        ],
        "combatants": [
            {
                "type": "player",  # 玩家角色：先攻/攻击靠中断报骰
                "controller": "user_aria",  # 操控者 user_id：中断据此推给该玩家
                "card": {
                    "id": "pc_aria", "name": "艾莉亚",
                    "strength": 16, "dexterity": 16, "constitution": 14,
                    "intelligence": 10, "wisdom": 12, "charisma": 13,
                    "max_hp": 30, "ac": 16, "level": 3,
                    "race": "人类", "char_class": "战士",
                    "save_proficiencies": ["strength", "constitution"],
                    "attacks": [
                        {"name": "长剑", "attack_bonus": 6, "damage_dice": "1d8+4",
                         "damage_type": "slashing", "range": "melee"},
                    ],
                    "skills": [
                        {"skill_id": "skill_second_wind", "charges": 1, "cooldown_left": 0},
                    ],
                    "inventory": [
                        {"item_id": "item_healing_potion", "quantity": 1},
                    ],
                },
            },
            {
                "type": "npc",  # NPC 盟友：玩家阵营，但由 DM(引擎) 自动掷骰
                "card": {
                    "id": "npc_bron", "name": "布隆",
                    "strength": 15, "dexterity": 12, "constitution": 14,
                    "max_hp": 24, "ac": 15, "level": 2,
                    "char_class": "牧师",
                    "attacks": [
                        {"name": "战锤", "attack_bonus": 5, "damage_dice": "1d8+3",
                         "damage_type": "bludgeoning", "range": "melee"},
                    ],
                },
            },
            {
                "type": "monster",  # 怪物：默认敌人阵营、DM 操控、引擎自动掷骰
                "card": {
                    "id": "goblin_1", "name": "哥布林·斥候",
                    "dexterity": 14, "max_hp": 12, "ac": 13, "initiative_bonus": 2,
                    "attacks": [
                        {"name": "弯刀", "attack_bonus": 4, "damage_dice": "1d6+2",
                         "damage_type": "slashing", "range": "melee"},
                    ],
                },
            },
            {
                "type": "monster",
                "card": {
                    "id": "goblin_2", "name": "哥布林·弓手",
                    "dexterity": 14, "max_hp": 10, "ac": 13, "initiative_bonus": 2,
                    "attacks": [
                        {"name": "短弓", "attack_bonus": 4, "damage_dice": "1d6+2",
                         "damage_type": "piercing", "range": "ranged"},
                    ],
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# 自动玩家：替前端回报每一个中断
# ---------------------------------------------------------------------------
def auto_respond(interrupt: dict) -> dict:
    """根据中断请求构造一份「恢复值」(Command(resume=...) 的载荷)。

    只有 `is_player_controlled` 的参战者才会产生中断；这里按中断类型回报：
    - 掷先攻 / 攻击检定 / 豁免 / 属性检定：报一个原始 d20（引擎自己加各种加值）；
    - 声明行动：从引擎给的合法选项里挑一个攻击目标（够不着就移动，没敌人就放弃）；
    - 伤害掷骰（重击）：留空，交引擎按「骰数翻倍」自动掷（保持可复现）。
    """
    kind = interrupt.get("interrupt_type")
    who = interrupt.get("directed_to", {})
    print(f"    >> 中断[{kind}] 面向 combatant={who.get('combatant_id')} user={who.get('user_id')}")
    print(f"      提示：{interrupt.get('prompt')}")

    if kind == InterruptType.DECLARE_ACTION.value:
        return _decide_player_action(interrupt.get("options") or {})

    if kind == InterruptType.DAMAGE_ROLL.value:
        # 重击补掷：不自报，交引擎按规则翻倍掷骰
        return {}

    # 其余皆为「报一个 d20 原始值」的中断：先攻 / 攻击检定 / 豁免 / 属性检定
    # 报较高的值，保证玩家这边大多命中、推动战斗收尾（信任边界：加值一律引擎算）。
    return {"d20": 18}


def _decide_player_action(options: dict) -> dict:
    """从合法选项里挑玩家这一回合的行动（简单策略：能打就打）。

    想测其它行动分支，把返回改成：
    - 技能：{"action_type": "skill", "skill_id": "skill_second_wind"}
    - 道具：{"action_type": "item", "item_id": "item_healing_potion", "target_id": "pc_aria"}
    - 创意：{"action_type": "improvise", "description": "掀翻火盆", "dc": 12, "ability": "strength"}
    - 移动：{"action_type": "move", "target_zone": "后排"}
    """
    print(f"      可选行动：攻击={_attack_summary(options.get('attack'))} "
          f"技能={options.get('skill')} 道具={options.get('item')} 移动={options.get('move')}")

    for weapon in options.get("attack", []):
        targets = weapon.get("targets") or []
        if targets:
            target = targets[0]
            print(f"      -> 决定：用「{weapon['attack_name']}」攻击 {target['name']}")
            return {
                "action_type": "attack",
                "attack_name": weapon["attack_name"],
                "target_id": target["id"],
            }

    # 没有够得着的敌人：能移动就挪过去，否则放弃这一回合
    moves = options.get("move") or []
    if moves:
        print(f"      -> 决定：移动到 {moves[0]['target_zone']}")
        return {"action_type": "move", "target_zone": moves[0]["target_zone"]}

    print("      -> 决定：放弃行动")
    return {"action_type": "pass"}


def _attack_summary(attack_options: list | None) -> str:
    """把攻击选项压成一行可读串，便于打印。"""
    parts = []
    for weapon in attack_options or []:
        names = "/".join(t["name"] for t in weapon.get("targets", [])) or "无目标"
        parts.append(f"{weapon['attack_name']}->[{names}]")
    return "，".join(parts) or "无"


# ---------------------------------------------------------------------------
# 驱动：从开局一路 submit 到结束
# ---------------------------------------------------------------------------
async def run_full_flow(max_steps: int = 200) -> dict:
    """跑完一整场战斗并返回最终统一负载（status=finished）。

    `max_steps` 是死循环兜底：正常这局远用不到，纯粹防止意外卡死。
    """
    engine = CombatEngine()
    room_id = "smoke_room"

    print("=" * 64)
    print("开始战斗：start_combat")
    print("=" * 64)
    payload = await engine.start_combat(room_id, build_scene())

    steps = 0
    while payload.get("status") == "interrupted":
        steps += 1
        if steps > max_steps:
            raise AssertionError(f"超过 {max_steps} 步仍未结束，疑似死循环")
        resume = auto_respond(payload["interrupt"])
        payload = await engine.submit(room_id, resume)

    return payload


def _print_combat_log(state: dict) -> None:
    """把全场日志逐条打印成可读轨迹（攻击/伤害/叙述/结算等）。"""
    print("\n" + "=" * 64)
    print("全场战斗日志（combat_log）")
    print("=" * 64)
    for entry in state.get("combat_log", []):
        rnd = entry.get("round")
        prefix = f"[R{rnd}]" if rnd is not None else "[--]"
        event = entry.get("event")
        if event == "narration":
            print(f"{prefix} [叙述] {entry.get('text')}")
        else:
            # 其余结构化事件原样打印关键字段
            shown = {k: v for k, v in entry.items() if k not in ("round",)}
            print(f"{prefix} [事件] {shown}")


def _print_final_state(state: dict) -> None:
    """打印最终参战者血量与结算写回。"""
    print("\n" + "=" * 64)
    print("最终战况")
    print("=" * 64)
    for cid, c in state.get("combatants", {}).items():
        flag = "存活" if c.is_alive else "倒下"
        print(f"  {c.name:<10}({cid}) HP={c.current_hp}/{c.max_hp} 阵营={c.faction.value} [{flag}]")

    scene = state.get("scene_context", {}) or {}
    print(f"\n  战利品 granted_loot = {scene.get('granted_loot')}")
    print(f"  写回 writeback = {scene.get('writeback')}")


async def _amain() -> dict:
    """异步主流程：跑全程 → 打印日志 / 终局 → 返回最终负载。"""
    payload = await run_full_flow()

    print("\n" + "=" * 64)
    print(f"战斗结束：status={payload['status']} result={payload.get('result')}")
    print("=" * 64)

    state = payload["state"]
    _print_combat_log(state)
    _print_final_state(state)
    return payload


def test_full_combat_flow() -> None:
    """pytest 入口：整场战斗应当能从开局一路跑到 finished，并产出结果与日志。

    无需 pytest-asyncio——用 asyncio.run 包一层即可在普通 pytest 下运行。
    """
    payload = asyncio.run(_amain())

    assert payload["status"] == "finished", f"战斗未正常结束：{payload}"
    assert payload.get("result") in ("players_win", "players_lose"), \
        f"结果应分出胜负，实际={payload.get('result')}"

    state = payload["state"]
    assert state.get("combat_log"), "战斗日志不应为空"
    # 结算节点应当落了一条 settle 事件
    assert any(e.get("event") == "settle" for e in state["combat_log"]), "缺少 settle 结算事件"


if __name__ == "__main__":
    asyncio.run(_amain())
