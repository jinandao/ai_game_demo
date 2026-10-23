"""行为域：多 NPC 的行为系统，负责推进时间与分发事件。"""

from __future__ import annotations

from ..infra.clock import Clock
from .context import Agent, EventSink, SpeechProvider, WorldEvent
from .director import BehaviorDirector
from .node import Node, Status
from .runner import BehaviorRunner
from .world import World


class BehaviorSystem:
    def __init__(
        self,
        world: World,
        director: BehaviorDirector,
        clock: Clock,
        *,
        speech: SpeechProvider | None = None,
        emit: EventSink | None = None,
        player_place: str = "outdoor",
    ) -> None:
        self.world = world
        self.director = director
        self.player_place = player_place
        self.player_xy: tuple[float, float] = (720.0, 660.0)
        self._clock = clock
        self._speech = speech
        self._emit = emit
        self._agents: dict[str, Agent] = {}
        self._runners: dict[str, BehaviorRunner] = {}

    # ── 注册 ──

    def spawn(
        self,
        npc_id: str,
        scene: str = "outdoor",
        xy: tuple[float, float] | None = None,
        speed: float = 150.0,
    ) -> BehaviorRunner:
        pos = xy or (720.0, 600.0)
        agent = Agent(npc_id=npc_id, place=scene, x=pos[0], y=pos[1], speed=speed)
        self._agents[npc_id] = agent
        runner = BehaviorRunner(
            agent,
            self.world,
            player_place=lambda: self.player_place,
            clock=self._clock,
            speech=self._speech,
            emit=self._emit,
        )
        self._runners[npc_id] = runner
        return runner

    def runner(self, npc_id: str) -> BehaviorRunner:
        return self._runners[npc_id]

    def agent(self, npc_id: str) -> Agent:
        return self._agents[npc_id]

    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    # ── 执行 ──

    def apply(self, npc_id: str, decision) -> Node:
        """决策 → 行为树 → 装载。已在跑的行为会被打断。"""
        return self.director.apply(self.runner(npc_id), decision)

    def update(self, dt: float) -> dict[str, Status]:
        return {npc_id: r.update(dt) for npc_id, r in self._runners.items()}

    def interrupt(self, npc_id: str, reason: str = "manual") -> None:
        self._runners[npc_id].interrupt(reason)

    def interrupt_all(self, reason: str = "manual") -> None:
        for r in self._runners.values():
            r.interrupt(reason)

    def resolve_invite(self, npc_id: str, accepted: bool) -> None:
        self._runners[npc_id].resolve_invite(accepted)

    # ── 查询 ──

    def snapshot(self) -> list[dict[str, object]]:
        return [r.snapshot() for r in self._runners.values()]

    def move_player(self, scene: str, xy: tuple[float, float] | None = None) -> None:
        self.player_place = scene
        if xy is not None:
            self.player_xy = (float(xy[0]), float(xy[1]))
        if self._emit:
            self._emit(
                WorldEvent("player_move", "player", {"scene": scene, "xy": self.player_xy})
            )
