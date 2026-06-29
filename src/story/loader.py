"""剧情圣经的加载与内存注册表。

决策 #2「按 campaign_id 单独引用存」：canon 大且只读，**不进每次 checkpoint**——
启动时从 ``canon/*.json`` 读盘、校验、反序列化为 :class:`~src.model.canon.Canon`，
缓存在进程内的注册表里；会话状态只持有 ``campaign_id`` 引用，需要时经注册表取回。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from src.model.canon import Canon, validate_canon

logger = logging.getLogger(__name__)

# 项目根下的 canon 目录（src/story/loader.py → 上溯两级到项目根）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANON_DIR = PROJECT_ROOT / "canon"


class CanonValidationError(ValueError):
    """canon 结构校验未通过（不放行，把「有头有尾」从祈祷变成编译期断言）。"""


def load_canon_file(path: str | Path) -> Canon:
    """读取单个 canon JSON 文件，构造并**校验** ``Canon``；不过校验则抛 :class:`CanonValidationError`。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    canon = Canon.from_dict(data)
    errors = validate_canon(canon)
    if errors:
        raise CanonValidationError(
            f"canon «{canon.campaign_id}»（{path.name}）校验未通过：\n- "
            + "\n- ".join(errors)
        )
    return canon


class CanonRegistry:
    """剧情圣经的内存注册表：``campaign_id → Canon``。线程安全。

    一个进程一份；多人/重启用持久化 checkpointer 时，进程启动后需重新 ``load_all`` 把注册表填满。
    """

    def __init__(self) -> None:
        self._by_id: dict[str, Canon] = {}
        self._lock = threading.Lock()

    def register(self, canon: Canon) -> Canon:
        """登记一个 canon（已构造好的对象，便于测试直接注入）。"""
        with self._lock:
            self._by_id[canon.campaign_id] = canon
        return canon

    def get(self, campaign_id: str) -> Canon | None:
        """按 campaign_id 取 canon，缺失返回 None。"""
        return self._by_id.get(campaign_id)

    def load_all(self, directory: str | Path = DEFAULT_CANON_DIR) -> dict[str, Canon]:
        """扫描目录下所有 ``*.json``，逐个加载+校验并登记，返回 ``campaign_id → Canon``。

        任一文件校验失败即抛错（宁可启动失败，也不放行一个走不到结局的剧本）。
        """
        directory = Path(directory)
        with self._lock:
            for path in sorted(directory.glob("*.json")):
                canon = load_canon_file(path)  # 校验失败会向上抛
                self._by_id[canon.campaign_id] = canon
                logger.info(
                    "[canon] 已加载 «%s» ← %s（%d 拍）",
                    canon.campaign_id,
                    path.name,
                    len(canon.beats),
                )
        return dict(self._by_id)


# 进程级单例（与 dm/knowledge.py 的 get_registry 风格一致）
_default_registry = CanonRegistry()


def get_registry() -> CanonRegistry:
    """获取默认 canon 注册表单例。"""
    return _default_registry
