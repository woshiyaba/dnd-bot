"""故事全链路·命令行交互冒烟脚本（真人驱动）。

验证需求文档那句话：**拿着这副骨架，让真模型把一整局冒险讲完——
开场 → 珠内自由探索 → 触发推进 → Boss 战 → 结局，读下来有头有尾、且好玩。**

运行（**需 DASHSCOPE_API_KEY**；DM 提示词走本地 knowledge/，不需 MySQL）::

    uv run python -m test.test_story_flow

交互约定：
- 平时直接输入你（玩家）想说/想做的事；输入 ``/quit`` 退出。
- 当引擎要你掷骰时，会打印中断提示：按需输入 d20 原始值（1-20）/伤害总和/行动声明；
  **加值一律引擎算**（守住「规则归引擎，骰子归玩家」）。

注意：这**不是** pytest 用例（名字带 test_ 只是沿用本目录习惯），它是个独立 CLI，需真实模型与人工输入。
"""

from __future__ import annotations

import asyncio
import json

from src.common.utils.log_util import ensure_logging_config
from src.session.engine import SessionEngine
from src.story.loader import get_registry

# 单人小队：一名 3 级战士（HP 18 / AC 15 / 长剑 +5, 1d8+3），足以与 22 HP 的 Boss 一战
PARTY = [
    {
        "type": "player",
        "controller": "u1",
        "card": {
            "id": "pc_aldous",
            "name": "奥德斯",
            "strength": 15, "dexterity": 13, "constitution": 14,
            "intelligence": 10, "wisdom": 12, "charisma": 11,
            "current_hp": 18, "max_hp": 18, "ac": 15,
            "initiative_bonus": 1,
            "race": "人类", "char_class": "战士", "level": 3,
            "attacks": [
                {"name": "长剑", "attack_bonus": 5, "damage_dice": "1d8+3", "damage_type": "slashing", "range": "melee"},
            ],
            "save_proficiencies": ["strength", "constitution"],
            "skill_proficiencies": ["athletics", "perception"],
        },
    },
]

SCENE_CONTEXT = {
    "campaign_id": "whispers_bell_tower",  # 本局剧本骨架（canon/whispers_bell_tower.json）
    "dm_mode": "llm",                      # 启用 LLM 版 DM（语义推进 + 叙述都靠真模型）
    "random_seed": 20240626,                # 可复现随机源
    "user_id": "u1",
    "party": PARTY,
}

ROOM_ID = "story_demo"


def _print_dm(say: str | None) -> None:
    """打印 DM 的话。"""
    if say:
        print(f"\nDM> {say}\n")


def _read_line(prompt: str) -> str | None:
    """读一行输入，EOF 视为退出。"""
    try:
        return input(prompt)
    except EOFError:
        return None


def _format_options(options: dict) -> str:
    """把战斗「声明行动」的合法选项压成可读文本。"""
    return json.dumps(options, ensure_ascii=False, indent=2)


async def _handle_interrupt(engine: SessionEngine, payload: dict) -> dict:
    """处理一次中断：按类型向玩家收集恢复值，再 submit 续跑。"""
    req = payload["interrupt"]
    itype = req.get("interrupt_type")
    directed = req.get("directed_to", {})
    print(f"\n[需要你出手] 中断类型={itype} 面向角色={directed.get('combatant_id')}（玩家={directed.get('user_id')}）")
    print("提示：", req.get("prompt"))
    if req.get("bonus"):
        print(f"（引擎会自动为你加值 +{req['bonus']}，你只报 d20 原始值）")

    if itype == "declare_action":
        print("可选行动：\n" + _format_options(req.get("options") or {}))
        action = (_read_line("action_type (attack/skill/item/move/improvise/pass)> ") or "").strip()
        target = (_read_line("target_id（没有就直接回车）> ") or "").strip()
        resume: dict = {"action_type": action or "pass"}
        if target:
            resume["target_id"] = target
    elif itype == "damage_roll":
        raw = (_read_line("输入伤害总和（按提示掷骰后的合计）> ") or "0").strip()
        resume = {"result": _safe_int(raw, 0)}
    else:  # roll_initiative / attack_roll / saving_throw / ability_check
        raw = (_read_line("输入 d20 原始值(1-20)> ") or "10").strip()
        resume = {"d20": _safe_int(raw, 10)}

    return await engine.submit(ROOM_ID, resume)


def _safe_int(text: str, default: int) -> int:
    """容错取整。"""
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


async def main() -> None:
    """开一局，驱动全链路直到结局拍。"""
    ensure_logging_config()
    registry = get_registry()
    registry.load_all()  # 加载 canon/*.json（校验失败会直接抛错）

    engine = SessionEngine()
    print("=== 钟楼下的低语 · 命令行试玩 ===")
    print("（直接输入你的行动；要掷骰时按提示报数；输入 /quit 退出）\n")

    result = await engine.start_session(ROOM_ID, SCENE_CONTEXT, opening="（冒险开始，请为我描述开场。）")

    while True:
        status = result.get("status")

        if status == "interrupted":
            result = await _handle_interrupt(engine, result)
            continue

        _print_dm(result.get("say"))

        if status == "finished":
            print(f"=== 本局冒险结束（结局拍：{result.get('ending_beat_id')}）===")
            break

        text = _read_line("你> ")
        if text is None or text.strip().lower() in {"/quit", "/exit", "quit", "exit"}:
            print("（退出试玩）")
            break
        result = await engine.message(ROOM_ID, text)


if __name__ == "__main__":
    asyncio.run(main())
