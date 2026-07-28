"""故事生产/加载层（离线一次性，**不在每回合热路径上**）。

依赖方向：``session → story → model``；``story`` 不认识 LangGraph。
本层是「提前定死」的剧情圣经（canon）的生产者与加载者：
- :mod:`src.story.loader` —— 从 ``canon/*.json`` 读盘、校验、反序列化为 ``Canon``，按 ``campaign_id`` 在内存注册表中引用。
- :mod:`src.story.prompt` —— 对话式故事访谈、确认门槛与 Canon 编译/修复规则。
- :mod:`src.story.generator` —— 使用真实 LLM 完成访谈、编译与校验修复。
"""

from src.story.loader import (
    CanonRegistry,
    CanonValidationError,
    get_registry,
    load_canon_file,
)
from src.story.prompt import (
    CANON_AUTHORING_RULE,
    STORY_INTERVIEW_RULE,
    build_canon_authoring_prompt,
    build_canon_repair_prompt,
    build_story_interview_prompt,
    validate_confirmed_design_brief,
)

__all__ = [
    "CANON_AUTHORING_RULE",
    "STORY_INTERVIEW_RULE",
    "CanonRegistry",
    "CanonValidationError",
    "build_canon_authoring_prompt",
    "build_canon_repair_prompt",
    "build_story_interview_prompt",
    "get_registry",
    "load_canon_file",
    "validate_confirmed_design_brief",
]
