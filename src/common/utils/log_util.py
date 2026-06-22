"""
日志工具。

统一日志格式和耗时计算，方便在各模块里直接复用。
"""

from __future__ import annotations

import logging
import time


def ensure_logging_config(level: int = logging.INFO) -> None:
    """确保日志已初始化，避免重复添加 handler。"""
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )

    root_logger.setLevel(level)


def get_elapsed_ms(start_time: float) -> float:
    """返回从 start_time 到现在经过的毫秒数。"""
    return round((time.perf_counter() - start_time) * 1000, 2)
