"""DM 知识库注册表。

扫描项目根 ``knowledge/`` 下的 Markdown 文档，解析 frontmatter 建立轻量目录，
正文**惰性读取**（首次 ``read`` 时才读盘并缓存），以此把"特定规则/怪物/技能"
做成 DM 可按需查阅的扩展，避免每轮把整本规则塞进提示词。

文档约定（见 docs/DM/00-DM节点扩展方案.md §3.1）：
- 文件名（去扩展名）即文档 id，例如 ``rules/group_check.md`` → id ``group_check``；
- frontmatter 写 ``name / category / tags / description``（不强制 ``id``）；
- ``description`` 是一句话摘要，进目录；正文只在 ``read`` 时返回。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# knowledge.py 位于 src/dm/ 下，向上两级是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"


@dataclass(slots=True)
class KnowledgeDoc:
    """一篇知识库文档的元信息（正文惰性加载）。"""

    doc_id: str            # 文档 id（取自文件名）
    name: str              # 显示名（frontmatter.name）
    category: str          # 分类：rule / monster / skill 等
    tags: list[str]        # 标签，用于检索
    description: str       # 一句话摘要，进目录
    path: Path             # 源文件路径
    _body: str | None = field(default=None, repr=False)  # 正文缓存

    def catalog_entry(self) -> dict:
        """导出目录项（轻量，不含正文），供 DM 浏览有哪些可查文档。"""
        return {
            "doc_id": self.doc_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
        }

    def body(self) -> str:
        """读取并缓存正文（首次调用才读盘，去掉 frontmatter 只留正文）。"""
        if self._body is None:
            _, self._body = _split_frontmatter(self.path.read_text(encoding="utf-8"))
        return self._body


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """切分 Markdown 的 YAML frontmatter，返回 ``(元数据, 正文)``。

    仅当文本以 ``---`` 起头时才解析；用 maxsplit=2 切分，保证正文里出现的
    ``---`` 不会被误当作分隔符。解析失败则视为无 frontmatter。
    """
    stripped = text.lstrip("﻿").lstrip()
    if not stripped.startswith("---"):
        return {}, text.strip()

    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:  # frontmatter 写错不致命，退化为无元数据
        logger.warning("[knowledge] frontmatter 解析失败：%s", exc)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, parts[2].strip()


class KnowledgeRegistry:
    """知识库注册表：扫描目录、维护目录索引、按需读取正文。

    线程安全：扫描与正文缓存用同一把锁保护（图节点可能在不同线程/协程调用）。
    """

    def __init__(self, root: Path = DEFAULT_KNOWLEDGE_DIR):
        """root 为知识库根目录，缺省项目根下的 ``knowledge/``。"""
        self._root = root
        self._docs: dict[str, KnowledgeDoc] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        """首次访问时扫描一次目录建立索引（线程安全的双检锁）。"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._scan()
            self._loaded = True

    def _scan(self) -> None:
        """扫描 ``knowledge/**/*.md``，解析 frontmatter，建立 id→文档 索引。"""
        docs: dict[str, KnowledgeDoc] = {}
        if not self._root.is_dir():
            logger.info("[knowledge] 知识库目录不存在：%s（视为空库）", self._root)
            self._docs = docs
            return

        for path in sorted(self._root.rglob("*.md")):
            doc_id = path.stem
            try:
                meta, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
            except OSError as exc:
                logger.warning("[knowledge] 读取失败，跳过 %s：%s", path, exc)
                continue
            # category 缺省取所在子目录名（rules/ → rule 由文档自己写，目录名作兜底）
            category = str(meta.get("category") or path.parent.name)
            tags = meta.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            docs[doc_id] = KnowledgeDoc(
                doc_id=doc_id,
                name=str(meta.get("name") or doc_id),
                category=category,
                tags=[str(t) for t in tags],
                description=str(meta.get("description") or ""),
                path=path,
            )
        logger.info("[knowledge] 已加载 %d 篇文档（根目录 %s）", len(docs), self._root)
        self._docs = docs

    def reload(self) -> None:
        """强制重新扫描（新增/修改 md 后调用，例如热更新）。"""
        with self._lock:
            self._scan()
            self._loaded = True

    def catalog(self, category: str | None = None) -> list[dict]:
        """返回目录（每篇一条摘要），可按分类过滤。供系统提示词常驻展示。"""
        self._ensure_loaded()
        return [
            d.catalog_entry()
            for d in self._docs.values()
            if category is None or d.category == category
        ]

    def search(self, query: str, category: str | None = None) -> list[dict]:
        """按关键词检索目录：匹配 id / 名称 / 摘要 / 标签的子串。

        query 为空时等价于 ``catalog``；返回目录项列表（不含正文）。
        """
        self._ensure_loaded()
        q = (query or "").strip().lower()
        results: list[dict] = []
        for d in self._docs.values():
            if category is not None and d.category != category:
                continue
            haystack = " ".join([d.doc_id, d.name, d.description, " ".join(d.tags)]).lower()
            if not q or q in haystack:
                results.append(d.catalog_entry())
        return results

    def read(self, doc_id: str) -> str:
        """返回某文档正文（带缓存）；找不到时返回友好提示而非抛错。"""
        self._ensure_loaded()
        doc = self._docs.get(doc_id)
        if doc is None:
            available = "、".join(sorted(self._docs)) or "（空）"
            return f"未找到文档「{doc_id}」。当前可用文档 id：{available}"
        return doc.body()


# 进程级单例：默认知识库注册表
_default_registry: KnowledgeRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> KnowledgeRegistry:
    """获取默认知识库注册表单例（懒加载）。"""
    global _default_registry
    if _default_registry is None:
        with _registry_lock:
            if _default_registry is None:
                _default_registry = KnowledgeRegistry()
    return _default_registry
