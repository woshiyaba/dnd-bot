"""故事生产/加载层（离线一次性，**不在每回合热路径上**）。

依赖方向：``session → story → model``；``story`` 不认识 LangGraph。
本层是「提前定死」的剧情圣经（canon）的生产者与加载者：
- :mod:`src.story.loader` —— 从 ``canon/*.json`` 读盘、校验、反序列化为 ``Canon``，按 ``campaign_id`` 在内存注册表中引用。
- ``src.story.generator``（LLM 编剧）留作二期，本版只手写 canon。
"""

from src.story.loader import (
    CanonRegistry,
    CanonValidationError,
    get_registry,
    load_canon_file,
)

__all__ = [
    "CanonRegistry",
    "CanonValidationError",
    "get_registry",
    "load_canon_file",
]
