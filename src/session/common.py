"""会话层共享的小工具：本局是否启用 LLM、世界事件流追加。

抽出到独立模块，供 ``dm_subgraph`` / ``story_nodes`` / ``graph`` 共用，避免相互 import 形成环。
"""

from __future__ import annotations

from src.model.dm_state import DMState


def llm_enabled(state: DMState) -> bool:
    """本局是否启用 LLM 版 DM。

    项目约定：DM 决策与叙述必须由真实 LLM 完成，不允许离线启发式模拟。
    ``state`` 参数保留给调用方统一签名；无论 scene 中写什么模式，都强制启用 LLM。
    """
    return True


def log_event(state: DMState, event: dict) -> list[dict]:
    """把一条世界事件追加进 campaign_log，返回新列表（会话层共用）。"""
    log = list(state.get("campaign_log", []))
    log.append(event)
    return log
