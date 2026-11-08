"""行为树执行器，每个 NPC 一个。"""

from __future__ import annotations

from collections.abc import Callable

from ..infra.clock import Clock
from .context import (
    ACTION_RESULT,
    Agent,
    BehaviorContext,
    EventSink,
    SpeechProvider,
    WorldEvent,
)
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
        player_xy: Callable[[], tuple[float, float]] | None = None,
        speech: SpeechProvider | None = None,
        emit: EventSink | None = None,
    ) -> None:
        self.agent = agent
        self.world = world
        self._player_place = player_place
        self._player_xy = player_xy
        self._clock = clock
        self._speech = speech
        self._emit = emit

        self.root: Node | None = None
        self.label: str = ""
        self.passive: bool = False
        # 软中断：True 时 update() 直接返回 RUNNING，树本身不动
        self.paused: bool = False
        self.pause_reason: str = ""
        self.trace: list[str] = []
        self.invite_result: bool | None = None
        # 最近一次"玩法结算"的判决：(成没成, 原因)。
        self.last_result: tuple[bool, str] | None = None

    # ── 装载 / 打断 ──

    def load(self, tree: Node, label: str = "", *, passive: bool = False) -> None:
        self.interrupt("replace")
        self.root = tree
        self.label = label
        self.passive = passive
        self.paused = False
        self.pause_reason = ""
        # 上一条的原因（"买绷带钱不够"被安到"修撬棍"头上）。
        self.last_result = None
        self.agent.busy = not passive
        self._log(f"load {label or tree.op}")
        if self._emit:
            self._emit(WorldEvent("behavior_start", self.agent.npc_id,
                                  {"label": label, "passive": passive}))

    def interrupt(self, reason: str = "manual") -> None:
        if self.root is None:
            self.paused = False
            return
        self._log(f"interrupt({reason})")
        self.root.reset()
        self.root = None
        self.passive = False
        self.paused = False
        self.pause_reason = ""
        # reset() 只会重置节点自己的游标，不会调 finish()
        self.agent.target = None
        self.agent.target_xy = None
        self.agent.walking = False
        self.agent.posture = "stand"
        self.agent.busy = False
        if self._emit:
            self._emit(
                WorldEvent("behavior_end", self.agent.npc_id,
                           {"label": self.label, "reason": reason})
            )

    # ── 软中断 ──

    def pause(self, reason: str = "chat") -> None:
        """冻住当前这棵树：不 tick、不重置、计时器原地停。"""
        if self.root is None or self.paused:
            return
        self.paused = True
        self.pause_reason = reason
        self.agent.target = None      # 别再往目标点走，站着听人说话
        self.agent.walking = False    # 只是停下动画；target_xy 留着，resume 继续走
        self._log(f"pause({reason})")
        if self._emit:
            self._emit(
                WorldEvent("behavior_pause", self.agent.npc_id,
                           {"label": self.label, "reason": reason})
            )

    def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        # 停在半路的走位要接着走，别停在"有目标点但不迈腿"的状态
        if self.root is not None and self.agent.target_xy is not None:
            self.agent.walking = True
        self._log("resume")
        if self._emit:
            self._emit(
                WorldEvent("behavior_resume", self.agent.npc_id,
                           {"label": self.label})
            )
        self.pause_reason = ""

    # ── 驱动 ──

    def update(self, dt: float) -> Status:
        if self.root is None:
            return Status.SUCCESS

        # 软中断：树留着，时间也不走。"走到一半的路""煮到一半的汤"都还在。
        if self.paused:
            return Status.RUNNING

        ctx = BehaviorContext(
            agent=self.agent,
            world=self.world,
            player_place=self._player_place(),
            player_xy=self._player_xy() if self._player_xy else (0.0, 0.0),
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

    # ── 外部结算（perform 等） ──

    def set_action_result(self, ok: bool, msg: str = "") -> None:
        """上层把"这件事成没成"写回来。"""
        self.last_result = (bool(ok), str(msg or ""))
        self.agent.blackboard[ACTION_RESULT] = self.last_result

    def take_result(self) -> tuple[bool, str] | None:
        """取走并清空最近一次判决。执行器读完就没人再需要它了。"""
        got = self.last_result
        self.last_result = None
        return got

    # ── 查询 ──

    @property
    def is_running(self) -> bool:
        return self.root is not None

    @property
    def is_occupied(self) -> bool:
        """真的在忙（不能被打断地重新决策）。"""
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
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "tree": self.root.to_dsl() if self.root else None,
        }

    def _log(self, line: str) -> None:
        stamp = self._clock.now().strftime("%H:%M:%S")
        self.trace.append(f"{stamp} {line}")
        if len(self.trace) > 200:
            del self.trace[:100]
