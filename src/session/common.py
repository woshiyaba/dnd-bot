"""会话层共享的小工具：本局是否启用 LLM、世界事件流追加。

抽出到独立模块，供 ``dm_subgraph`` / ``story_nodes`` / ``graph`` 共用，避免相互 import 形成环。
"""

from __future__ import annotations

from src.model.dm_state import DMState


def llm_enabled(state: DMState) -> bool:
    """本局是否启用 LLM 版 DM：由会话主图在 scene 里写入的 ``dm_mode`` 决定。

    放在会话层判断（主图启动时已校验过 API Key）；这里只读开关，缺省启发式。
    """
    return (state.get("scene") or {}).get("dm_mode") == "llm"


def log_event(state: DMState, event: dict) -> list[dict]:
    """把一条世界事件追加进 campaign_log，返回新列表（会话层共用）。"""
    log = list(state.get("campaign_log", []))
    log.append(event)
    return log
