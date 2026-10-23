"""记忆域：记忆检索。嵌入不可用时退回关键词。

阈值按嵌入模型标定；低于 sim_floor 的条目直接丢弃。
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from ..domain.models import Hit, MemoryItem
from ..infra.clock import Clock
from ..infra.config import Config
from ..infra.embedding import Embedder, EmbedError, cosine
from .store import MemoryStore

_CJK = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")

# 仅在关键词兜底路径下使用。
_SYNONYMS: dict[str, set[str]] = {
    "睡不着": {"失眠", "睡眠"},
    "失眠": {"睡不着", "睡眠"},
    "茶": {"喝茶", "安神"},
    "外面": {"站外", "出闸", "废墟", "城里"},
    "残响": {"听见", "耳机", "深区"},
}

EMBED_CHARS = 400


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        cfg: Config,
        clock: Clock,
        embedder: Embedder | None = None,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._clock = clock
        self._embedder = embedder
        self._vec: dict[str, list[float]] = {}
        self._degraded = False      # 掉线一次就别再逐轮重试

    # ── 主入口 ──

    def retrieve(
        self, query: str, *, player_id: str = "", npc_id: str, k: int | None = None
    ) -> list[Hit]:
        k = k or self._cfg.memory_top_k
        items = self._store.items(npc_id)
        if not items:
            return []

        hits = self._vector_hits(query, items, k)
        if hits is not None:
            return hits
        return self._keyword_hits(query, items, k)

    def unresolved(self, player_id: str, npc_id: str, k: int = 3) -> list[MemoryItem]:
        """决策层专用：要的不是"相关"，是"待办"。按重要性排，不看新近度。"""
        items = self._store.promises(npc_id)
        return sorted(items, key=lambda m: m.importance, reverse=True)[:k]

    def recent(self, npc_id: str, k: int = 6) -> list[MemoryItem]:
        """最近做过的事。决策层拿它来避免"做完又做一遍"。"""
        return self._store.recent(npc_id, k)

    # ── 向量路径 ──

    def _vector_hits(
        self, query: str, items: list[MemoryItem], k: int
    ) -> list[Hit] | None:
        """返回 None 表示"这条路走不通，请用关键词"。"""
        if self._embedder is None or self._degraded or not query.strip():
            return None

        try:
            self._ensure_vectors(items)
            qvec = self._embedder.embed([query])[0]
        except EmbedError:
            self._degraded = True
            return None

        now = self._clock.now()
        scored: list[tuple[float, float, MemoryItem]] = []
        for item in items:
            vec = self._vec.get(item.id)
            if vec is None:
                continue
            sim = cosine(qvec, vec)
            if sim < self._cfg.sim_floor:
                continue        # 不够像，就当没想起来
            # 语义为主；重要性和新近度只做微调，避免旧的约定永远压过新话题
            bonus = 0.06 * (item.importance / 5.0) + 0.04 * self._recency(item, now)
            scored.append((sim + bonus, sim, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(
                text=f"{m.key}：{m.value}",
                score=round(total, 4),
                source=f"memory:{m.id}",
                meta={
                    "importance": m.importance,
                    "subject": m.subject.value,
                    "promise": m.promise,
                    "evidence": m.evidence,
                    "sim": round(sim, 4),
                },
            )
            for total, sim, m in scored[:k]
        ]

    def _ensure_vectors(self, items: list[MemoryItem]) -> None:
        """只给还没算过的条目算向量。记忆是增量的，每次只会补几条。"""
        missing = [m for m in items if m.id not in self._vec]
        if not missing:
            return
        texts = [f"{m.key}：{m.value}"[:EMBED_CHARS] for m in missing]
        vectors = self._embedder.embed(texts)  # type: ignore[union-attr]
        for m, vec in zip(missing, vectors):
            self._vec[m.id] = vec

    # ── 关键词兜底 ──

    def _keyword_hits(self, query: str, items: list[MemoryItem], k: int) -> list[Hit]:
        terms = _terms(query)
        if not terms:
            return []
        now = self._clock.now()
        scored = [(self._score(m, terms, now), m) for m in items]
        scored = [(s, m) for s, m in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(
                text=f"{m.key}：{m.value}",
                score=round(s, 4),
                source=f"memory:{m.id}",
                meta={
                    "importance": m.importance,
                    "subject": m.subject.value,
                    "promise": m.promise,
                    "evidence": m.evidence,
                    "degraded": True,
                },
            )
            for s, m in scored[:k]
        ]

    def _score(self, item: MemoryItem, terms: set[str], now: datetime) -> float:
        match = _match(item, terms)
        if match <= 0:
            return 0.0
        boost = 1.25 if item.promise else 1.0
        return (item.importance / 5.0) * self._recency(item, now) * match * boost

    def _recency(self, item: MemoryItem, now: datetime) -> float:
        days = max(0.0, (now - item.ts).total_seconds() / 86400)
        return math.exp(-days / self._cfg.recency_half_life_days)

    # ── 调试 ──

    def stats(self) -> dict[str, object]:
        return {
            "embedder": self._embedder is not None and not self._degraded,
            "degraded": self._degraded,
            "cached": len(self._vec),
        }


def _terms(text: str) -> set[str]:
    raw = set(_CJK.findall(text))
    out = {t for t in raw if len(t) > 1 or "\u4e00" <= t <= "\u9fff"}
    for t in list(out):
        out |= _SYNONYMS.get(t, set())
    return out


def _match(item: MemoryItem, terms: set[str]) -> float:
    haystack = f"{item.key}{item.value}"
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t and t in haystack)
    return min(1.0, hits / max(1, len(terms)) + (0.3 if item.key in haystack else 0))
