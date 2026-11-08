"""多 NPC 的行为系统。"""

from __future__ import annotations

from ..infra.clock import Clock
from .context import Agent, EventSink, SpeechProvider, WorldEvent
from .director import BehaviorDirector
from .executor import ActionExecutor
from .node import Status
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
        self._executors: dict[str, ActionExecutor] = {}
        # 动作执行出岔子时的上报口：签名 (npc_id, 原因) -> None。
        self.on_trouble: object = None
        # "这件事真的没成"。由 core 接上（当场重开一次决策）。
        self.on_failed: object = None

    # ── 注册 ──

    def spawn(
        self,
        npc_id: str,
        scene: str = "outdoor",
        xy: tuple[float, float] | None = None,
        speed: float = 150.0,
    ) -> BehaviorRunner:
        pos = xy
        if pos is None:
            # 没指定位置就放在场景入口附近，按 npc_id 分左右，别叠在一起。
            sc = self.world.scene(scene) if self.world.has(scene) else None
            if sc is not None:
                side = 120.0 if npc_id == "A" else -120.0
                pos = (sc.entry[0] + side, sc.entry[1] - 80.0)
            else:
                pos = (720.0, 600.0)
        agent = Agent(npc_id=npc_id, place=scene, x=pos[0], y=pos[1], speed=speed)
        self._agents[npc_id] = agent
        runner = BehaviorRunner(
            agent,
            self.world,
            player_place=lambda: self.player_place,
            clock=self._clock,
            player_xy=lambda: self.player_xy,
            speech=self._speech,
            emit=self._emit,
        )
        self._runners[npc_id] = runner

        # 重新 spawn（读档换场景、NPC 换位置）时，把原来那个执行器里的
        old = self._executors.get(npc_id)
        if old is not None:
            old.rebind(runner, self.director)
            self._executors[npc_id] = old
        else:
            self._executors[npc_id] = ActionExecutor(npc_id, runner, self.director)
        self._executors[npc_id].on_trouble = self.on_trouble
        self._executors[npc_id].on_failed = self.on_failed
        return runner

    def runner(self, npc_id: str) -> BehaviorRunner:
        return self._runners[npc_id]

    def executor(self, npc_id: str) -> ActionExecutor:
        return self._executors[npc_id]

    def agent(self, npc_id: str) -> Agent:
        return self._agents[npc_id]

    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    # ── 执行 ──

    def apply(self, npc_id: str, decision) -> None:
        """接一个目的：拆成动作序列交给执行器，执行器逐条装树。"""
        self._executors[npc_id].load(decision)

    def update(self, dt: float) -> dict[str, Status]:
        return {npc_id: ex.update(dt) for npc_id, ex in self._executors.items()}

    def interrupt(self, npc_id: str, reason: str = "manual") -> None:
        self._executors[npc_id].clear(reason)

    def interrupt_all(self, reason: str = "manual") -> None:
        for ex in self._executors.values():
            ex.clear(reason)

    # ── 软中断（对话 / 剧情） ──

    def pause(self, npc_id: str, reason: str = "chat") -> None:
        ex = self._executors.get(npc_id)
        if ex is not None:
            ex.pause(reason)

    def resume(self, npc_id: str) -> None:
        ex = self._executors.get(npc_id)
        if ex is not None:
            ex.resume()

    def pause_all(self, reason: str = "chat") -> None:
        for ex in self._executors.values():
            ex.pause(reason)

    def resume_all(self) -> None:
        for ex in self._executors.values():
            ex.resume()

    def resolve_invite(self, npc_id: str, accepted: bool) -> None:
        self._runners[npc_id].resolve_invite(accepted)

    def set_action_result(self, npc_id: str, ok: bool, msg: str = "") -> None:
        """上层结算完了，把结果交回行为层。"""
        runner = self._runners.get(npc_id)
        if runner is not None:
            runner.set_action_result(ok, msg)

    # ── 查询 ──

    def is_idle(self, npc_id: str) -> bool:
        """她手上没有待执行的动作——可以去申请新目的了。"""
        ex = self._executors.get(npc_id)
        return ex is None or ex.is_idle

    def snapshot(self) -> list[dict[str, object]]:
        out = []
        for npc_id, runner in self._runners.items():
            item = runner.snapshot()
            ex = self._executors.get(npc_id)
            if ex is not None:
                item["plan"] = {
                    "intent": ex.plan.intent,
                    "goal": ex.plan.goal_text,
                    "progress": ex.progress,
                    "paused": ex.plan.paused,
                }
            out.append(item)
        return out

    # ── 计划存档 ──

    def plans_snapshot(self) -> dict:
        """所有 NPC 的执行进度。存进存档，读档后能接着做。"""
        return {
            npc_id: ex.snapshot()
            for npc_id, ex in self._executors.items()
            if not ex.is_idle or ex.is_paused
        }

    def plans_restore(self, data: dict) -> int:
        """按存档重建。"""
        if not isinstance(data, dict):
            return 0
        done = 0
        for npc_id, raw in data.items():
            ex = self._executors.get(npc_id)
            if ex is not None and ex.restore(raw):
                done += 1
        return done

    def move_player(self, scene: str, xy: tuple[float, float] | None = None) -> None:
        self.player_place = scene
        if xy is not None:
            self.player_xy = (float(xy[0]), float(xy[1]))
        if self._emit:
            self._emit(
                WorldEvent("player_move", "player", {"scene": scene, "xy": self.player_xy})
            )
