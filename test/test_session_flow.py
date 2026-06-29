"""会话全流程冒烟测试（中央 DM ↔ 战斗子图）。

把 ``SessionEngine`` 当一整局冒险驱动，演示并验证：

    开场叙述 → 玩家做一件不确定的事(明检定·中断) → 触发战斗(进战斗子图)
    → 战斗逐骰中断到分出胜负 → DM 叙述战后 → 等玩家下一步

驱动方式（统一负载，见 src/session/engine.py）：
- start_session / message → {status: awaiting_input | interrupted}
- submit(resume)          → 下一个中断点 / 回合结束

可离线跑：DM 默认走**启发式**（不需 DASHSCOPE），同 ``random_seed`` 下结果可复现。

运行方式（仓库根目录）::

    uv run python test/test_session_flow.py
    uv run python -m pytest test/test_session_flow.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.session.engine import SessionEngine  # noqa: E402
from src.model.enums import InterruptType  # noqa: E402


# ---------------------------------------------------------------------------
# 场景：1 名玩家 在一处神殿前厅，场上有一只敌意哥布林
# ---------------------------------------------------------------------------
def build_scene() -> dict:
    """构造一局冒险的初始上下文（启发式 DM，可离线复现）。"""
    return {
        "dm_mode": "llm",  # 离线跑；接入 LLM 时改 "llm"
        "random_seed": 20260626,
        "user_id": "user_aria",
        "scene": {
            "location": "废弃神殿·前厅",
            "description": "霉味弥漫，断裂的石柱间散落着碎石，一只哥布林正盯着你。",
            "actors": [
                {
                    "actor_id": "goblin_1",
                    "name": "哥布林·斥候",
                    "disposition": "hostile",
                    "type": "monster",
                    "card": {
                        "id": "goblin_1",
                        "name": "哥布林·斥候",
                        "dexterity": 14,
                        "max_hp": 12,
                        "ac": 13,
                        "initiative_bonus": 2,
                        "attacks": [
                            {
                                "name": "弯刀",
                                "attack_bonus": 4,
                                "damage_dice": "1d6+2",
                                "damage_type": "slashing",
                                "range": "melee",
                            },
                        ],
                    },
                },
            ],
            "exits": ["东门", "地下室"],
            "threat": "哥布林虎视眈眈",
        },
        "loot_table": [{"item_id": "item_gold", "quantity": 30}],
        "party": [
            {
                "type": "player",
                "controller": "user_aria",
                "card": {
                    "id": "pc_aria",
                    "name": "艾莉亚",
                    "strength": 16,
                    "dexterity": 16,
                    "constitution": 14,
                    "intelligence": 12,
                    "wisdom": 12,
                    "charisma": 13,
                    "max_hp": 30,
                    "ac": 16,
                    "level": 3,
                    "race": "人类",
                    "char_class": "战士",
                    "save_proficiencies": ["strength", "constitution"],
                    "attacks": [
                        {
                            "name": "长剑",
                            "attack_bonus": 6,
                            "damage_dice": "1d8+4",
                            "damage_type": "slashing",
                            "range": "melee",
                        },
                    ],
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# 自动玩家：统一回报所有中断（DM 明检定 + 战斗骰）
# ---------------------------------------------------------------------------
def auto_respond(interrupt: dict) -> dict:
    """根据中断类型构造恢复值（同 docs/战斗/03 协议）。"""
    kind = interrupt.get("interrupt_type")
    who = interrupt.get("directed_to", {})
    print(
        f"    >> 中断[{kind}] 面向 {who.get('combatant_id')}/{who.get('user_id')}：{interrupt.get('prompt')}"
    )

    if kind == InterruptType.DECLARE_ACTION.value:
        return _decide_action(interrupt.get("options") or {})
    if kind == InterruptType.DAMAGE_ROLL.value:
        return {}  # 重击补掷交引擎
    # 先攻 / 攻击 / 豁免 / 属性检定：报一个较高的 d20（加值引擎算）
    return {"d20": 18}


def _decide_action(options: dict) -> dict:
    """战斗里玩家声明行动：能打就打，够不着就移动，没敌人就放弃。"""
    for weapon in options.get("attack", []):
        targets = weapon.get("targets") or []
        if targets:
            print(f"      -> 用「{weapon['attack_name']}」攻击 {targets[0]['name']}")
            return {
                "action_type": "attack",
                "attack_name": weapon["attack_name"],
                "target_id": targets[0]["id"],
            }
    moves = options.get("move") or []
    if moves:
        return {"action_type": "move", "target_zone": moves[0]["target_zone"]}
    return {"action_type": "pass"}


async def _drive_to_rest(
    engine: SessionEngine, room_id: str, payload: dict, max_steps: int = 200
) -> dict:
    """一路 submit，直到本步回到 awaiting_input（中途的中断全自动回报）。"""
    steps = 0
    while payload.get("status") == "interrupted":
        steps += 1
        if steps > max_steps:
            raise AssertionError("超过步数上限，疑似死循环")
        payload = await engine.submit(room_id, auto_respond(payload["interrupt"]))
    return payload


# ---------------------------------------------------------------------------
# 全流程
# ---------------------------------------------------------------------------
async def run_flow() -> dict:
    engine = SessionEngine()
    room = "smoke_session"

    print("=" * 64)
    print("① 开场")
    print("=" * 64)
    payload = await engine.start_session(room, build_scene(), opening="我打量四周。")
    payload = await _drive_to_rest(engine, room, payload)
    print(f"  DM：{payload.get('say')}")
    assert payload["status"] == "awaiting_input"

    print("\n" + "=" * 64)
    print("② 玩家做一件不确定的事 → 明检定（中断）")
    print("=" * 64)
    payload = await engine.message(room, "我仔细搜索这些碎石和石柱，看看有没有线索。")
    assert (
        payload["status"] == "interrupted"
    ), f"应触发明检定中断，实际={payload['status']}"
    assert payload["interrupt"]["interrupt_type"] == InterruptType.ABILITY_CHECK.value
    payload = await _drive_to_rest(engine, room, payload)
    print(f"  检定结果：{payload.get('last_check')}")
    print(f"  DM：{payload.get('say')}")
    assert payload["status"] == "awaiting_input"
    assert payload["last_check"] and "success" in payload["last_check"]

    print("\n" + "=" * 64)
    print("③ 玩家开战 → 进战斗子图 → 逐骰打完")
    print("=" * 64)
    payload = await engine.message(room, "我拔剑冲上去攻击那只哥布林！")
    assert (
        payload["status"] == "interrupted"
    ), f"应进入战斗并停在第一个骰，实际={payload['status']}"
    payload = await _drive_to_rest(engine, room, payload)
    print(f"  战斗结算：{payload.get('last_combat')}")
    print(f"  DM 战后：{payload.get('say')}")
    assert payload["status"] == "awaiting_input"

    return payload


def _print_state(state: dict) -> None:
    print("\n" + "=" * 64)
    print("终局世界状态")
    print("=" * 64)
    print(f"  战斗结果 last_combat = {state.get('last_combat')}")
    for cid, c in (state.get("party") or {}).items():
        print(f"  队伍 {c.name}({cid}) HP={c.current_hp}/{c.max_hp} 存活={c.is_alive}")
    scene = state.get("scene") or {}
    print(f"  场上在场者 = {[a.get('name') for a in scene.get('actors', [])]}")
    print(f"  事件流条数 = {len(state.get('campaign_log', []))}")


async def _amain() -> dict:
    payload = await run_flow()
    _print_state(payload["state"])
    return payload


def test_session_flow() -> None:
    """整局应当：明检定能跑通、战斗能打完、战后回到等待玩家输入，且敌人被清除。"""
    payload = asyncio.run(_amain())
    state = payload["state"]

    assert payload["status"] == "awaiting_input"
    # 战斗确实发生且玩家胜
    assert (state.get("last_combat") or {}).get(
        "outcome"
    ) == "players_win", f"应玩家获胜，实际={state.get('last_combat')}"
    # 被击败的哥布林已从场景移除
    assert all(
        a.get("actor_id") != "goblin_1" for a in (state["scene"].get("actors", []))
    ), "战败的哥布林应已从场景中移除"
    # 事件流非空，且含一条 combat 事件
    assert any(
        e.get("event") == "combat" for e in state.get("campaign_log", [])
    ), "缺少 combat 事件"


if __name__ == "__main__":
    asyncio.run(_amain())
