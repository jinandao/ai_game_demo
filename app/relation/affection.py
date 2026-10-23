"""关系域：好感度状态机，唯一修改 affinity 与 stage 的地方。"""

from __future__ import annotations

from datetime import date, datetime

from ..domain.enums import Stage
from ..domain.models import AffectionHint, AffectionState, Profile
from ..infra.clock import Clock
from ..infra.config import Config

# (阈值下界, 阶段)。从高到低匹配。
STAGE_TABLE: list[tuple[int, Stage]] = [
    (60, Stage.DEPENDENT),
    (40, Stage.AMBIGUOUS),
    (25, Stage.CLOSE),
    (10, Stage.ACQUAINTANCE),
    (0, Stage.STRANGER),
]


class AffectionEngine:
    def __init__(self, cfg: Config, clock: Clock) -> None:
        self._cfg = cfg
        self._clock = clock

    # ── 查询 ──

    def stage_of(self, affinity: int) -> Stage:
        for floor, stage in STAGE_TABLE:
            if affinity >= floor:
                return stage
        return Stage.STRANGER

    def state_of(self, profile: Profile, npc_id: str) -> AffectionState:
        return profile.affection.setdefault(npc_id, AffectionState())

    # ── 写入（仅此三类入口） ──

    def apply_hints(self, profile: Profile, npc_id: str, hints: list[AffectionHint]) -> int:
        """抽取产物 → 实际数值。受每日上限约束，返回实际变化量。"""
        total = 0
        for h in hints:
            total += self._apply(profile, npc_id, h.delta, h.reason)
        return total

    def apply_node(self, profile: Profile, npc_id: str, delta: int, reason: str) -> int:
        """关键节点选项。节点加分不受每日闲聊上限约束（它是一次性的）。"""
        st = self.state_of(profile, npc_id)
        before = st.affinity
        st.affinity = max(0, min(100, before + delta))
        st.stage = self.stage_of(st.affinity)
        return st.affinity - before

    def _apply(self, profile: Profile, npc_id: str, delta: int, reason: str) -> int:
        st = self.state_of(profile, npc_id)
        today = self._clock.now().date()
        if st.last_gain != today:
            st.last_gain = today
            st.daily_gain = 0

        if delta > 0:
            room = max(0, self._cfg.max_daily_affection - st.daily_gain)
            delta = min(delta, room)
            st.daily_gain += delta

        before = st.affinity
        st.affinity = max(0, min(100, st.affinity + delta))
        st.stage = self.stage_of(st.affinity)
        return st.affinity - before

    # ── 时间 ──

    def decay(self, profile: Profile, now: datetime) -> None:
        if self._cfg.decay_per_day <= 0:
            return
        for st in profile.affection.values():
            if st.last_gain is None:
                continue
            days = (now.date() - st.last_gain).days
            if days > 0:
                st.affinity = max(0, st.affinity - int(days * self._cfg.decay_per_day))
                st.stage = self.stage_of(st.affinity)

    # ── 跨阶段事件 ──

    def crossed(self, old: int, new: int) -> list[Stage]:
        """返回跨越的阶段，供 UI 弹提示 / 解锁动作。"""
        old_s, new_s = self.stage_of(old), self.stage_of(new)
        if old_s == new_s:
            return []
        order = [s for _, s in reversed(STAGE_TABLE)]
        i, j = order.index(old_s), order.index(new_s)
        return order[min(i, j) + 1 : max(i, j) + 1] if j > i else []

    def debug_set(self, profile: Profile, npc_id: str, value: int) -> None:
        """调试面板用。演示时走不到高阶段，整个解锁机制就等于没演示过。"""
        st = self.state_of(profile, npc_id)
        st.affinity = max(0, min(100, value))
        st.stage = self.stage_of(st.affinity)
