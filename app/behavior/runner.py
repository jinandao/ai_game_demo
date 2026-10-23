"""行为域：行为树执行器，每个 NPC 一个。"""

from __future__ import annotations

from collections.abc import Callable

from ..infra.clock import Clock
from .context import Agent, BehaviorContext, EventSink, SpeechProvider, WorldEvent
from .node import Node, Status
from .world import World


class BehaviorRunner:
    def __init__(
        self,
        agent: Agent,
        world: World,
        *,
        player_place: Callable[[], str],
        clock: Clock,
        speech: SpeechProvider | None = None,
        emit: EventSink | None = None,
    ) -> None:
        self.agent = agent
        self.world = world
        self._player_place = player_place
        self._clock = clock
        self._speech = speech
        self._emit = emit

        self.root: Node | None = None
        self.label: str = ""
        self.passive: bool = False
        self.trace: list[str] = []
        self.invite_result: bool | None = None

    # ── 装载 / 打断 ──

    def load(self, tree: Node, label: str = "", *, passive: bool = False) -> None:
        self.interrupt("replace")
        self.root = tree
        self.label = label
        self.passive = passive
        # passive（idle/rest/enjoy_view）不算"忙"，否则它永远 RUNNING，
        # 会把 NPC 锁死在这棵树里，再也接不到新决策。
        self.agent.busy = not passive
        self._log(f"load {label or tree.op}")
        if self._emit:
            self._emit(WorldEvent("behavior_start", self.agent.npc_id,
                                  {"label": label, "passive": passive}))

    def interrupt(self, reason: str = "manual") -> None:
        if self.root is None:
            return
        self._log(f"interrupt({reason})")
        self.root.reset()
        self.root = None
        self.passive = False
        self.agent.target = None
        self.agent.busy = False
        if self._emit:
            self._emit(
                WorldEvent("behavior_end", self.agent.npc_id,
                           {"label": self.label, "reason": reason})
            )

    # ── 驱动 ──

    def update(self, dt: float) -> Status:
        if self.root is None:
            return Status.SUCCESS

        ctx = BehaviorContext(
            agent=self.agent,
            world=self.world,
            player_place=self._player_place(),
            dt=dt,
            now=self._clock.now(),
            speech=self._speech,
            emit=self._emit,
        )
        status = self.root.tick(ctx)

        if status is Status.RUNNING:
            return status

        self._log(f"{self.label or 'tree'} -> {status.value}")
        self.root = None
        self.passive = False
        self.agent.busy = False
        if self._emit:
            self._emit(
                WorldEvent("behavior_end", self.agent.npc_id,
                           {"label": self.label, "reason": status.value})
            )
        return status

    # ── 外部答复（Invite 等） ──

    def resolve_invite(self, accepted: bool) -> None:
        self.invite_result = accepted
        self.agent.blackboard["invite_result"] = accepted

    # ── 查询 ──

    @property
    def is_running(self) -> bool:
        return self.root is not None

    @property
    def is_occupied(self) -> bool:
        """真的在忙；passive 行为不算，否则 NPC 无法重新决策。"""
        return self.root is not None and not self.passive

    def snapshot(self) -> dict[str, object]:
        return {
            "npc_id": self.agent.npc_id,
            "place": self.agent.place,
            "target": self.agent.target,
            "posture": self.agent.posture,
            "busy": self.agent.busy,
            "label": self.label,
            "running": self.is_running,
            "passive": self.passive,
            "tree": self.root.to_dsl() if self.root else None,
        }

    def _log(self, line: str) -> None:
        stamp = self._clock.now().strftime("%H:%M:%S")
        self.trace.append(f"{stamp} {line}")
        if len(self.trace) > 200:
            del self.trace[:100]
