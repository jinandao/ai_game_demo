"""记忆域：持久化。全部记忆存为一个 JSON 数组，进游戏加载、退游戏保存。

dedupe=True 的条目按 key 去重，决策流水等 dedupe=False 的条目全部保留。
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ..domain.enums import Subject
from ..domain.models import AffectionState, MemoryItem, Profile
from ..infra.config import Config
from ..infra.storage import JsonFile


def new_memory_id() -> str:
    return f"m_{uuid.uuid4().hex[:10]}"


class MemoryStore(ABC):
    @abstractmethod
    def load_from_disk(self) -> None: ...

    @abstractmethod
    def save_to_disk(self) -> None: ...

    @abstractmethod
    def items(self, npc_id: str) -> list[MemoryItem]:
        """有效记忆：已应用 supersedes、已按 key 去重。"""

    @abstractmethod
    def append(self, items: list[MemoryItem]) -> int: ...

    @abstractmethod
    def has_range(self, session_id: str, turn_range: tuple[int, int]) -> bool: ...


class JsonMemoryStore(MemoryStore):
    def __init__(self, cfg: Config, player_id: str) -> None:
        self._cfg = cfg
        self._player_id = player_id
        self._items: list[MemoryItem] = []
        self._ranges: set[tuple[str, tuple[int, int]]] = set()
        self._loaded = False

    # ── 生命周期 ──

    def load_from_disk(self) -> None:
        raw = JsonFile(self._cfg.player_memory_path(self._player_id)).load()
        rows = raw if isinstance(raw, list) else raw.get("items", [])
        self._items = [_from_row(r) for r in rows if isinstance(r, dict)]
        self._ranges = {(m.session_id, m.turn_range) for m in self._items}
        self._loaded = True

    def save_to_disk(self) -> None:
        if not self._loaded:
            return
        payload = [_to_row(m) for m in self._items]
        JsonFile(self._cfg.player_memory_path(self._player_id)).save(payload)  # type: ignore[arg-type]

    # ── 写 ──

    def append(self, items: list[MemoryItem]) -> int:
        n = 0
        for item in items:
            key = (item.session_id, item.turn_range)
            if key in self._ranges:
                continue          # 幂等：同一个 (会话, 区间) 只落一次
            self._items.append(item)
            self._ranges.add(key)
            n += 1
        return n

    # ── 读 ──

    def items(self, npc_id: str) -> list[MemoryItem]:
        scoped = [m for m in self._items if m.npc_id == npc_id]
        superseded = {m.supersedes for m in scoped if m.supersedes}

        kept: list[MemoryItem] = []
        by_key: dict[str, MemoryItem] = {}
        for m in scoped:
            if m.id in superseded:
                continue
            if not m.dedupe:
                kept.append(m)          # 行为流水：全留
                continue
            prev = by_key.get(m.key)
            if prev is None or m.ts >= prev.ts:
                by_key[m.key] = m

        return sorted(kept + list(by_key.values()), key=lambda m: m.ts, reverse=True)

    def recent(self, npc_id: str, k: int = 6) -> list[MemoryItem]:
        """最近 k 条，不做去重、不看 importance。用来告诉决策层"我刚做过什么"。"""
        scoped = [m for m in self._items if m.npc_id == npc_id]
        return sorted(scoped, key=lambda m: m.ts, reverse=True)[:k]

    def promises(self, npc_id: str) -> list[MemoryItem]:
        return [m for m in self.items(npc_id) if m.promise]

    def has_range(self, session_id: str, turn_range: tuple[int, int]) -> bool:
        return (session_id, turn_range) in self._ranges

    # ── 调试 ──

    def stats(self) -> dict[str, Any]:
        by_subject: dict[str, int] = {}
        for m in self._items:
            by_subject[m.subject.value] = by_subject.get(m.subject.value, 0) + 1
        return {"total": len(self._items), "by_subject": by_subject}


class ProfileStore:
    """L4。只负责收发，不负责算——计算全在 AffectionEngine。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def load(self, player_id: str, name: str = "玩家") -> Profile:
        raw = JsonFile(self._cfg.profile_path(player_id)).load()
        if not raw:
            return Profile(player_id=player_id, name=name)
        return Profile(
            player_id=raw.get("player_id", player_id),
            name=raw.get("name", name),
            affection={
                npc: AffectionState(
                    affinity=s.get("affinity", 0),
                    daily_gain=s.get("daily_gain", 0),
                )
                for npc, s in raw.get("affection", {}).items()
            },
            flags=raw.get("flags", {}),
            last_session_at=_parse_ts(raw.get("last_session_at")),
        )

    def save(self, profile: Profile) -> None:
        JsonFile(self._cfg.profile_path(profile.player_id)).save(
            {
                "player_id": profile.player_id,
                "name": profile.name,
                "affection": {
                    npc: {
                        "affinity": st.affinity,
                        "stage": st.stage.value,
                        "daily_gain": st.daily_gain,
                        "last_gain": st.last_gain.isoformat() if st.last_gain else None,
                    }
                    for npc, st in profile.affection.items()
                },
                "flags": profile.flags,
                "last_session_at": profile.last_session_at.isoformat()
                if profile.last_session_at
                else None,
            }
        )


# ── 序列化 ──


def _to_row(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "session_id": item.session_id,
        "npc_id": item.npc_id,
        "turn_range": list(item.turn_range),
        "ts": item.ts.isoformat(),
        "subject": item.subject.value,
        "key": item.key,
        "value": item.value,
        "importance": item.importance,
        "evidence": item.evidence,
        "source": item.source,
        "promise": item.promise,
        "dedupe": item.dedupe,
        "supersedes": item.supersedes,
    }


def _from_row(r: dict[str, Any]) -> MemoryItem:
    subject_raw = str(r.get("subject", "other"))
    try:
        subject = Subject(subject_raw)
    except ValueError:
        subject = Subject.OTHER      # 兼容旧数据里的 world / unresolved / raw

    return MemoryItem(
        id=r["id"],
        session_id=r.get("session_id", ""),
        npc_id=r.get("npc_id", "A"),
        turn_range=tuple(r.get("turn_range", (0, 0))),  # type: ignore[arg-type]
        ts=datetime.fromisoformat(r["ts"]) if r.get("ts") else datetime.now(),
        subject=subject,
        key=r.get("key", ""),
        value=r.get("value", ""),
        importance=int(r.get("importance", 3)),
        evidence=r.get("evidence", ""),
        source=r.get("source", "dialogue"),
        promise=bool(r.get("promise", r.get("subject") == "unresolved")),
        dedupe=bool(r.get("dedupe", True)),
        supersedes=r.get("supersedes"),
    )


def _parse_ts(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
