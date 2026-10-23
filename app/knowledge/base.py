"""知识域：世界观库的抽象与向量实现。

权限过滤在打分之前完成：私密块只有主人能检索到。
两个实现：KeywordKB 作为兜底，VectorKB 为主路径，嵌入不可用时退回前者。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..domain.enums import Scope
from ..domain.models import Hit
from ..infra.embedding import Embedder, EmbedError, cosine


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    scope: Scope
    owner: str | None = None          # CHARACTER 范围时的归属 npc_id
    visibility: str = "public"        # public | private
    tags: tuple[str, ...] = field(default_factory=tuple)


def is_visible(chunk: Chunk, npc_id: str | None) -> bool:
    """私密块只有主人能检索到。任何检索实现都必须先过这一关。"""
    if chunk.scope is not Scope.CHARACTER:
        return True
    if chunk.visibility == "private":
        return chunk.owner == npc_id
    return True


class KnowledgeBase(ABC):
    @abstractmethod
    def retrieve(
        self, query: str, *, scope: Scope, npc_id: str | None = None, k: int = 3
    ) -> list[Hit]: ...


class VectorKB(KnowledgeBase):
    """嵌入检索。sim_floor 需按模型标定；嵌入失败后退回 fallback，且不再重试。"""

    EMBED_CHARS = 800                 # 太长的块截断，嵌入模型对长度敏感

    def __init__(
        self,
        chunks: list[Chunk],
        embedder: Embedder | None = None,
        *,
        sim_floor: float = 0.45,
        fallback: KnowledgeBase | None = None,
    ) -> None:
        self._chunks = chunks
        self._embedder = embedder
        self._floor = sim_floor
        self._fallback = fallback
        self._vec: list[list[float] | None] = [None] * len(chunks)
        self._ready = False
        self._reason = "未初始化"

    # ── 建索引 ──

    def warmup(self) -> bool:
        """启动时调一次。返回是否真的建成了向量索引。"""
        if self._embedder is None:
            self._reason = "未配置嵌入模型"
            return False
        if not self._chunks:
            self._reason = "没有可索引的条目"
            return False

        texts = [c.text[: self.EMBED_CHARS] for c in self._chunks]
        try:
            vectors = self._embedder.embed(texts)
        except EmbedError as e:
            self._reason = f"嵌入失败：{e}"
            return False

        if len(vectors) != len(texts):
            self._reason = "向量数量与条目不符"
            return False

        self._vec = list(vectors)
        self._ready = True
        self._reason = "ok"
        return True

    # ── 检索 ──

    def retrieve(
        self, query: str, *, scope: Scope, npc_id: str | None = None, k: int = 3
    ) -> list[Hit]:
        if not self._ready:
            return self._fallback_retrieve(query, scope, npc_id, k)

        try:
            qvec = self._embedder.embed([query])[0] if self._embedder else []
        except EmbedError:
            self._ready = False          # 掉线就彻底放弃，不再逐轮重试
            self._reason = "嵌入服务掉线"
            return self._fallback_retrieve(query, scope, npc_id, k)

        scored: list[tuple[float, int]] = []
        for i, chunk in enumerate(self._chunks):
            if chunk.scope is not scope:
                continue
            if not is_visible(chunk, npc_id):
                continue              # 权限过滤先于打分
            vec = self._vec[i]
            if vec is None:
                continue
            sim = cosine(qvec, vec)
            if sim >= self._floor:    # 低于阈值的直接丢，宁可不答也别硬凑
                scored.append((sim, i))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(
                text=self._chunks[i].text,
                score=round(s, 4),
                source=f"kb:{self._chunks[i].id}",
                meta={
                    "scope": self._chunks[i].scope.value,
                    "owner": self._chunks[i].owner,
                    "sim": round(s, 4),
                },
            )
            for s, i in scored[:k]
        ]

    def _fallback_retrieve(
        self, query: str, scope: Scope, npc_id: str | None, k: int
    ) -> list[Hit]:
        if self._fallback is None:
            return []
        return self._fallback.retrieve(query, scope=scope, npc_id=npc_id, k=k)

    # ── 调试 ──

    def stats(self) -> dict[str, object]:
        return {
            "ready": self._ready,
            "chunks": len(self._chunks),
            "dim": len(self._vec[0]) if self._ready and self._vec and self._vec[0] else 0,
            "floor": self._floor,
            "reason": self._reason,
        }
