"""决策队列，攒批、串行、可暂停。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable

from ..domain.enums import DecisionIntent
from ..domain.models import Decision, NpcEvent, NpcState, Profile
from .planner import DecisionPlanner
from .view import WorldView

MAX_BATCH = 4

class DecisionQueue:
    def __init__(
        self,
        planner: DecisionPlanner,
        *,
        max_batch: int = MAX_BATCH,
        on_results: Callable[[dict[str, NpcEvent]], None] | None = None,
    ) -> None:
        self._planner = planner
        self._max_batch = max_batch
        self._on_results = on_results

        self._pending: dict[str, tuple[Profile, NpcState, str, str, WorldView]] = {}
        self._lock = threading.Lock()
        self._busy = False
        self._paused = False
        # 代数：`discard()` 自增。正在跑的那一批拿的是旧代数，结果会被丢掉
        self._epoch = 0

        # 调试用
        self._runs = 0
        self._last_batch = 0

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decide")

    # ── 配置 ──

    def set_sink(self, callback: Callable[[dict[str, NpcEvent]], None]) -> None:
        self._on_results = callback

    def pause(self) -> None:
        """对话开启时调用。"""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def discard(self) -> None:
        """丢掉待跑的和正在跑的这批决策（重置 / 读档后用）。"""
        with self._lock:
            self._pending.clear()
            self._epoch += 1

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def busy(self) -> bool:
        return self._busy

    # ── 入队 ──

    def submit(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        here: str = "",
        player_at: str = "",
        view: WorldView | None = None,
    ) -> None:
        with self._lock:
            # 同一个 NPC 只保留最新一份：攒了一分钟再排队的旧决策没有意义
            self._pending[npc_id] = (
                profile, state, here, player_at, view or WorldView(here=here, player_at=player_at)
            )

    # ── 驱动 ──

    def tick(self) -> None:
        """每帧调一次。暂停中或正在跑都不发新批。"""
        if self._paused or self._busy:
            return
        if not self._pending:
            return

        with self._lock:
            if self._paused or self._busy or not self._pending:
                return
            items = list(self._pending.items())[: self._max_batch]
            for npc_id, _ in items:
                self._pending.pop(npc_id, None)
            self._busy = True
            self._runs += 1
            self._last_batch = len(items)
            epoch = self._epoch

        self._executor.submit(self._run, dict(items), epoch)

    def _run(
        self,
        batch: dict[str, tuple[Profile, NpcState, str, str, WorldView]],
        epoch: int = 0,
    ) -> None:
        started = time.monotonic()
        events: dict[str, NpcEvent]
        try:
            events = self._planner.decide_many(batch)
        except Exception:  # noqa: BLE001  队列不能因为一次模型异常就停摆
            events = {
                npc_id: NpcEvent(
                    npc_id=npc_id,
                    decision=Decision(DecisionIntent.IDLE, "queue_error", 0.0),
                )
                for npc_id in batch
            }

        self._log(batch, events, time.monotonic() - started, self._runs)

        # 这一批跑的时候被 discard 过（重置 / 读档）：结果作废。
        if epoch != self._epoch:
            with self._lock:
                self._busy = False
            return

        try:
            if events and self._on_results is not None:
                self._on_results(events)
        finally:
            with self._lock:
                self._busy = False

    @staticmethod
    def _log(
        batch: dict[str, tuple[Profile, NpcState, str, str, WorldView]],
        events: dict[str, NpcEvent],
        cost: float,
        round_no: int = 0,
    ) -> None:
        """把每次决策打到控制台。"""
        head = f"[决策] 第 {round_no} 轮 · {len(batch)} 个 · {cost:.1f}s"
        lines = [head]
        for npc_id in batch:
            event = events.get(npc_id)
            if event is None:
                lines.append(f"  {npc_id} → （无结果）")
                continue
            d = event.decision
            goal = d.goal_text or d.intent.value
            why = f"（{d.reason}）" if d.reason else ""
            lines.append(f"  {npc_id} 目标：{goal}{why}")
            if d.plan_text:
                lines.append(f"       打算：{' → '.join(d.plan_text[:4])}")
        print("\n".join(lines), flush=True)

    # ── 调试 ──

    def stats(self) -> dict[str, object]:
        with self._lock:
            pending = len(self._pending)
        return {
            "pending": pending,
            "busy": self._busy,
            "paused": self._paused,
            "runs": self._runs,
            "last_batch": self._last_batch,
        }

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
