"""记忆检索。"""

from __future__ import annotations

import re

from ..domain.enums import MemoryTier
from ..domain.models import Hit, MemoryItem
from ..infra.clock import Clock
from ..infra.config import Config
from ..infra.embedding import Embedder, EmbedError, cosine
from .store import MemoryStore

_CJK = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")

EMBED_CHARS = 400

# 决策层写进深刻记忆的「互动」条目是步骤流水
_STEP_LOG_PREFIX = "我为玩家做过："

def is_step_log(value: str) -> bool:
    text = (value or "").strip()
    return text.startswith(_STEP_LOG_PREFIX) or " → " in text

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
        self,
        query: str,
        *,
        player_id: str = "",
        npc_id: str,
        k: int | None = None,
        # 默认只查这两档：玩家画像 + 深刻记忆。
        tiers: tuple[MemoryTier, ...] = (MemoryTier.INTERACTION, MemoryTier.DEEP),
    ) -> list[Hit]:
        """语义检索：每条记忆各自和问题算余弦，排序取前 k。"""
        k = k or self._cfg.memory_top_k
        items = self._store.items(npc_id)
        if tiers is not None:
            wanted = set(tiers)
            items = [m for m in items if m.tier in wanted]
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

    def events(self, npc_id: str, k: int = 5) -> list[MemoryItem]:
        """最近的动作流水：她刚才去做了什么。"""
        return self._store.by_tier(npc_id, MemoryTier.EVENT, k)

    def deep(self, npc_id: str, k: int = 5) -> list[MemoryItem]:
        """深刻记忆：剧情里发生过的 + 她为他做过的事。"""
        return self._store.by_tier(npc_id, MemoryTier.DEEP, k)

    def interactions(self, npc_id: str, k: int = 5) -> list[MemoryItem]:
        """对话里提取的、关于玩家的事（不删的那一档）。"""
        return self._store.by_tier(npc_id, MemoryTier.INTERACTION, k)

    def recent(self, npc_id: str, k: int = 6) -> list[MemoryItem]:
        """最近做过的事。决策层拿它来避免"做完又做一遍"。"""
        return self._store.recent(npc_id, k)

    def forget(self) -> None:
        """丢掉向量缓存。"""
        self._vec.clear()

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

        scored: list[tuple[float, MemoryItem]] = []
        for item in items:
            vec = self._vec.get(item.id)
            if vec is None:
                continue
            sim = cosine(qvec, vec)
            if sim < self._cfg.sim_floor:
                continue        # 不够像，就当没想起来
            scored.append((sim, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(
                text=f"{m.key}：{m.value}",
                score=round(sim, 4),
                source=f"memory:{m.id}",
                meta={
                    "importance": m.importance,
                    "subject": m.subject.value,
                    "promise": m.promise,
                    "evidence": m.evidence,
                    "sim": round(sim, 4),
                },
            )
            for sim, m in scored[:k]
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
        scored = [(_match(m, terms), m) for m in items]
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

    # ── 调试 ──

    def stats(self) -> dict[str, object]:
        return {
            "embedder": self._embedder is not None and not self._degraded,
            "degraded": self._degraded,
            "cached": len(self._vec),
        }

def _terms(text: str) -> set[str]:
    """切词：中文按单字，英文数字按整段。只保留有信息量的（去掉单字母）。"""
    return {t for t in set(_CJK.findall(text)) if len(t) > 1 or "\u4e00" <= t <= "\u9fff"}

def _match(item: MemoryItem, terms: set[str]) -> float:
    """命中率 = 问题里的词有多少出现在这条记忆里。0~1，不掺任何权重。"""
    haystack = f"{item.key} {item.value}"
    hits = sum(1 for t in terms if t and t in haystack)
    return hits / max(1, len(terms))
