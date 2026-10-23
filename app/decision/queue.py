"""决策队列：攒批、串行、可暂停。

同一帧提交的请求合成一批；对话开启时暂停，避免和聊天抢本地模型。
submit / tick 在 UI 线程，_run 在 worker 线程，结果由调用方切回 UI 线程再改状态。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable

from ..domain.enums import DecisionIntent
from ..domain.models import Decision, NpcEvent, NpcState, Profile
from .planner import DecisionPlanner

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

        self._pending: dict[str, tuple[Profile, NpcState, str, str]] = {}
        self._lock = threading.Lock()
        self._busy = False
        self._paused = False

        # 调试用
        self._runs = 0
        self._last_batch = 0

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decide")

    # ── 配置 ──

    def set_sink(self, callback: Callable[[dict[str, NpcEvent]], None]) -> None:
        self._on_results = callback

    def pause(self) -> None:
        """对话开启时调用。已经在跑的那一批拦不住（本地请求不能 abort），
        但不会再发新的。"""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

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
    ) -> None:
        with self._lock:
            # 同一个 NPC 只保留最新一份：攒了一分钟再排队的旧决策没有意义
            self._pending[npc_id] = (profile, state, here, player_at)

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

        self._executor.submit(self._run, dict(items))

    def _run(self, batch: dict[str, tuple[Profile, NpcState, str, str]]) -> None:
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

        self._log(batch, events, time.monotonic() - started)

        try:
            if events and self._on_results is not None:
                self._on_results(events)
        finally:
            with self._lock:
                self._busy = False

    @staticmethod
    def _log(
        batch: dict[str, tuple[Profile, NpcState, str, str]],
        events: dict[str, NpcEvent],
        cost: float,
    ) -> None:
        """把每次决策打到控制台。reason 非空表示走了兜底。"""
        head = f"[决策] {len(batch)} 个 · {cost:.1f}s"
        lines = [head]
        for npc_id in batch:
            event = events.get(npc_id)
            if event is None:
                lines.append(f"  {npc_id} → （无结果）")
                continue
            tag = event.decision.reason or "模型"
            lines.append(f"  {npc_id} → {event.decision.intent.value:<14} ({tag})")
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
