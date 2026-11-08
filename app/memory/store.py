"""记忆持久化。"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ..domain.enums import MemoryTier, Subject
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

    @abstractmethod
    def clear(self) -> None:
        """清空全部记忆并落盘（重置存档用）。"""

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

    def clear(self) -> None:
        """清空记忆，并立刻把空数组写回文件。"""
        self._items = []
        self._ranges = set()
        # 没 load 过也要能写：重置本身就是一次全新的开始
        self._loaded = True
        self.save_to_disk()

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
        if n:
            self._trim()
        return n

    # ── 按档裁剪 ──

    def _trim(self) -> None:
        """有上限的档位丢最旧的。"""
        plans = (
            (MemoryTier.EVENT, int(self._cfg.event_memory_cap), ""),
            (MemoryTier.DEEP, int(self._cfg.deep_action_memory_cap), "story"),
        )
        drop: set[int] = set()
        for tier, cap, excluded_source in plans:
            if cap <= 0:
                continue
            idx = [
                i
                for i, m in enumerate(self._items)
                if m.tier == tier and m.source != excluded_source
            ]
            if len(idx) > cap:
                drop.update(idx[: len(idx) - cap])
        if not drop:
            return

        kept: list[MemoryItem] = []
        for i, m in enumerate(self._items):
            if i in drop:
                # 幂等键一起清掉，否则同一条以后再也写不进来
                self._ranges.discard((m.session_id, m.turn_range))
                continue
            kept.append(m)
        self._items = kept

    # ── 读 ──

    def items(self, npc_id: str) -> list[MemoryItem]:
        """这个 NPC 的全部记忆，按时间倒序。"""
        rows = list(enumerate(m for m in self._items if m.npc_id == npc_id))
        # 同一 ts 时按写入顺序倒序：冻结时钟下"最近"才不会变成"最早"
        rows.sort(key=lambda pair: (pair[1].ts, pair[0]), reverse=True)
        return [m for _i, m in rows]

    def grouped(self, npc_id: str) -> dict[str, list[MemoryItem]]:
        """按 key 分组：`{"家庭成员": [...], "偏好": [...]}`。"""
        out: dict[str, list[MemoryItem]] = {}
        for m in self.items(npc_id):
            out.setdefault(m.key, []).append(m)
        return out

    def recent(self, npc_id: str, k: int = 6) -> list[MemoryItem]:
        """最近 k 条，给决策层看"我刚做过什么"。"""
        return self.items(npc_id)[:k]

    def by_tier(self, npc_id: str, tier: MemoryTier, k: int = 5) -> list[MemoryItem]:
        """某一档最近的 k 条（已按 key 去重、按新近排好）。"""
        out = [m for m in self.items(npc_id) if m.tier == tier]
        return out[:k] if k > 0 else out

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
        "tier": item.tier.value,
    }

def _tier_of(r: dict[str, Any]) -> MemoryTier:
    """老存档没有 tier 字段，按 source 反推：不能默认全塞进一档。"""
    raw = str(r.get("tier") or "").strip()
    if raw:
        try:
            return MemoryTier(raw)
        except ValueError:
            pass
    source = str(r.get("source") or "")
    key = str(r.get("key") or "")
    if source == "decision":
        return MemoryTier.EVENT if key.startswith("决策_") else MemoryTier.DEEP
    if source in ("story", "action"):
        return MemoryTier.DEEP
    return MemoryTier.INTERACTION

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
        tier=_tier_of(r),
    )

def _parse_ts(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
