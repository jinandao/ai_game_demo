"""知识域：关键词检索实现。demo 阶段的默认选择。"""

from __future__ import annotations

import re

from ..domain.enums import Scope
from ..domain.models import Hit
from .base import Chunk, KnowledgeBase, is_visible

_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]+")


class KeywordKB(KnowledgeBase):
    def __init__(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks

    def retrieve(
        self, query: str, *, scope: Scope, npc_id: str | None = None, k: int = 3
    ) -> list[Hit]:
        terms = set(_TOKEN.findall(query))
        if not terms:
            return []

        scored: list[tuple[float, Chunk]] = []
        for c in self._chunks:
            if c.scope is not scope:
                continue
            if not is_visible(c, npc_id):
                continue  # 权限过滤先于打分，这一步不能被跳过
            s = self._score(c, terms)
            if s > 0:
                scored.append((s, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(text=c.text, score=round(s, 4), source=f"kb:{c.id}",
                meta={"scope": c.scope.value, "owner": c.owner})
            for s, c in scored[:k]
        ]

    @staticmethod
    def _score(c: Chunk, terms: set[str]) -> float:
        hits = sum(1 for t in terms if t in c.text or t in c.tags)
        if not hits:
            return 0.0
        # 命中比例 + 标签精确命中的额外权重
        return min(1.0, hits / len(terms)) + (0.2 if any(t in c.tags for t in terms) else 0)
