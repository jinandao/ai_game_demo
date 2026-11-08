"""标准行为库，按 op 名注册。"""

from __future__ import annotations

import math
import random

from ..domain.enums import ActionId
from .behavior import Behavior, EmptyBehavior
from .context import ACTION_RESULT, BehaviorContext, SpeakRequest, WorldEvent
from .node import Status
from .world import PLAYER

class MoveTo(Behavior):
    """移动到场景内的坐标，或跨场景移动。"""

    op = "move"
    ARRIVE_EPS = 6.0

    def __init__(
        self,
        place: str = "?",
        x: float | None = None,
        y: float | None = None,
        speed: float | None = None,
        to_prop: str = "",
        **kw,
    ) -> None:
        super().__init__(**kw)
        self.place = place
        self.x = x
        self.y = y
        self.speed = speed
        self.to_prop = to_prop
        self.params.update(place=place)
        if x is not None and y is not None:
            self.params.update(x=x, y=y)
        if to_prop:
            self.params.update(to_prop=to_prop)

        self._route: list[str] = []
        self._idx = 0
        self._dest_xy: tuple[float, float] = (0.0, 0.0)
        self._next_scene: str | None = None

    def start(self, ctx: BehaviorContext) -> None:
        dest = ctx.world.resolve(
            str(self.place), agent_place=ctx.agent.place, player_place=ctx.player_place
        )
        self._route = ctx.world.path(ctx.agent.place, dest)
        self._idx = 0
        self._dest_xy = (
            (float(self.x), float(self.y))
            if self.x is not None and self.y is not None
            else tuple(float(v) for v in ctx.world.entry(dest))  # type: ignore[assignment]
        )

        # 找玩家：他已经在这个场景里的话，走到他身边，而不是走到场景入口。
        if str(self.place) == PLAYER and dest == ctx.agent.place:
            px, py = ctx.player_xy
            if px or py:
                self._dest_xy = (px + 38.0, py + 26.0)

        # 走到某个道具旁边：灶台、工作台、铁柜……
        if self.to_prop:
            scene = ctx.world.scene(dest)
            prop = next((p for p in scene.props if p.id == self.to_prop), None)
            if prop is not None:
                cx, cy = prop.center
                self._dest_xy = (float(cx), float(cy + prop.h // 2 + 28))

        if self.speed:
            ctx.agent.speed = float(self.speed)
        ctx.agent.target = dest
        ctx.agent.walking = True
        ctx.agent.posture = "walk"
        self._aim(ctx)

        ctx.signal(
            WorldEvent(
                "move_start",
                ctx.agent.npc_id,
                {"from": ctx.agent.place, "to": dest, "xy": self._dest_xy},
            )
        )

    def _aim(self, ctx: BehaviorContext) -> None:
        if self._idx >= len(self._route) - 1:
            self._next_scene = None
            ctx.agent.target_xy = self._dest_xy
        else:
            nxt = self._route[self._idx + 1]
            self._next_scene = nxt
            ctx.agent.target_xy = tuple(  # type: ignore[assignment]
                float(v) for v in ctx.world.door(ctx.agent.place, nxt)
            )

    def update(self, ctx: BehaviorContext) -> Status:
        tx, ty = ctx.agent.target_xy or (ctx.agent.x, ctx.agent.y)
        dx, dy = tx - ctx.agent.x, ty - ctx.agent.y
        dist = math.hypot(dx, dy)

        if dist <= self.ARRIVE_EPS:
            if self._next_scene:
                ctx.agent.place = self._next_scene
                ex, ey = ctx.world.entry(self._next_scene)
                ctx.agent.x, ctx.agent.y = float(ex), float(ey)
                self._idx += 1
                ctx.signal(
                    WorldEvent("scene_enter", ctx.agent.npc_id, {"scene": self._next_scene})
                )
                self._aim(ctx)
                return Status.RUNNING
            return Status.SUCCESS

        step = min(dist, ctx.agent.speed * max(0.0, ctx.dt))
        ctx.agent.x += dx / dist * step
        ctx.agent.y += dy / dist * step
        ctx.agent.facing = _facing(dx, dy)
        return Status.RUNNING

    def finish(self, ctx: BehaviorContext) -> None:
        ctx.agent.target = None
        ctx.agent.target_xy = None
        ctx.agent.walking = False
        ctx.agent.posture = "stand"
        ctx.signal(
            WorldEvent(
                "move_end",
                ctx.agent.npc_id,
                {"at": ctx.agent.place, "xy": (ctx.agent.x, ctx.agent.y)},
            )
        )

def _facing(dx: float, dy: float) -> str:
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"

class Wander(Behavior):
    """在当前场景里随便走一小段。"""

    op = "wander"

    def __init__(self, radius: float = 220.0, **kw) -> None:
        super().__init__(**kw)
        self.radius = radius
        self.params.update(radius=radius)
        self._move: MoveTo | None = None

    def start(self, ctx: BehaviorContext) -> None:
        ax, ay = ctx.agent.x, ctx.agent.y
        x = ax + random.uniform(-self.radius, self.radius)
        y = ay + random.uniform(-self.radius * 0.5, self.radius * 0.5)
        # 会让两个人永远只在左上角那一块转，走到大厅另一头就看不到了。
        left, top, right, bottom = ctx.world.bounds(ctx.agent.place)
        x = max(left, min(right, x))
        y = max(top, min(bottom, y))

        self._move = MoveTo(place=ctx.agent.place, x=x, y=y)
        self._move.start(ctx)

    def update(self, ctx: BehaviorContext) -> Status:
        if self._move is None:
            return Status.SUCCESS
        st = self._move.update(ctx)
        return Status.SUCCESS if st is Status.SUCCESS else Status.RUNNING

    def finish(self, ctx: BehaviorContext) -> None:
        if self._move is not None:
            self._move.finish(ctx)
            self._move = None

class Say(Behavior):
    """说一句。只说 topic，台词由对话层生成——行为层不编话。"""

    op = "say"

    def __init__(self, topic: str = "greet", about: str | None = None,
                 hold: float = 0.0, **kw) -> None:
        super().__init__(**kw)
        self.topic = topic
        self.about = about
        self.hold = hold
        self.params.update(topic=topic, hold=hold)
        self._left = 0.0

    def start(self, ctx: BehaviorContext) -> None:
        ctx.speak(SpeakRequest(npc_id=ctx.agent.npc_id, topic=self.topic, about=self.about))
        self._left = float(self.hold)

    def update(self, ctx: BehaviorContext) -> Status:
        if self._left <= 0:
            return Status.SUCCESS
        self._left -= ctx.dt
        return Status.RUNNING if self._left > 0 else Status.SUCCESS

class Invite(Behavior):
    """发起邀请。"""

    op = "invite"

    def __init__(self, action: str = ActionId.INVITE_WALK.value,
                 await_consent: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.action = action
        self.await_consent = await_consent
        self.params.update(action=action, await_consent=await_consent)

    def start(self, ctx: BehaviorContext) -> None:
        ctx.agent.blackboard.pop("invite_result", None)
        ctx.speak(
            SpeakRequest(
                npc_id=ctx.agent.npc_id,
                topic="invite",
                action=str(self.action),
            )
        )
        ctx.signal(
            WorldEvent(
                "invite",
                ctx.agent.npc_id,
                {"action": str(self.action), "await_consent": bool(self.await_consent)},
            )
        )

    def update(self, ctx: BehaviorContext) -> Status:
        if not self.await_consent:
            return Status.SUCCESS
        result = ctx.agent.blackboard.get("invite_result")
        if result is None:
            return Status.RUNNING
        return Status.SUCCESS if result else Status.FAILURE

class Offer(Behavior):
    """提议为他做一件事。"""

    op = "offer"

    def __init__(self, action: str = "", **kw) -> None:
        super().__init__(**kw)
        self.action = action
        self.params.update(action=action)

    def start(self, ctx: BehaviorContext) -> None:
        ctx.speak(
            SpeakRequest(
                npc_id=ctx.agent.npc_id, topic="offer", action=str(self.action)
            )
        )
        ctx.signal(
            WorldEvent("offer", ctx.agent.npc_id, {"action": str(self.action)})
        )

    def update(self, ctx: BehaviorContext) -> Status:
        return Status.SUCCESS

class Deliver(Behavior):
    """交付：把刚做完的事交到他手上，然后重新开口。"""

    op = "deliver"

    def __init__(self, action: str = "", outcome: str = "", **kw) -> None:
        super().__init__(**kw)
        self.action = action
        self.outcome = outcome
        self.params.update(action=action)
        if outcome:
            self.params.update(outcome=outcome)

    def start(self, ctx: BehaviorContext) -> None:
        ctx.speak(
            SpeakRequest(
                npc_id=ctx.agent.npc_id, topic="delivery", action=str(self.action)
            )
        )
        ctx.signal(
            WorldEvent(
                "delivery",
                ctx.agent.npc_id,
                {"action": str(self.action), "outcome": str(self.outcome)},
            )
        )

    def update(self, ctx: BehaviorContext) -> Status:
        return Status.SUCCESS

class Wait(Behavior):
    op = "wait"

    def __init__(self, seconds: float = 2.0, **kw) -> None:
        super().__init__(**kw)
        self.seconds = seconds
        self.params.update(seconds=seconds)
        self._left = 0.0

    def start(self, ctx: BehaviorContext) -> None:
        self._left = float(self.seconds)

    def update(self, ctx: BehaviorContext) -> Status:
        self._left -= ctx.dt
        return Status.RUNNING if self._left > 0 else Status.SUCCESS

class Work(Behavior):
    """干一会儿活，然后把结果交给上层。"""

    op = "perform"

    # 等判决的上限。上层在同一帧的 drain_events 里就写回来了
    RESULT_TIMEOUT = 0.5

    def __init__(
        self,
        seconds: float = 6.0,
        kind: str = "",
        posture: str = "",
        event: str = "npc_action",
        payload: dict | None = None,
        wait_result: bool = False,
        **kw,
    ) -> None:
        super().__init__(**kw)
        self.seconds = float(seconds)
        self.kind = kind or str(self.op)
        self.posture = posture
        self.event = event
        self.payload = dict(payload or {})
        self.wait_result = bool(wait_result)
        # 失败原因。执行器在 FAILURE 时读它，写进 skipped / 上报给决策层。
        self.reason = ""
        self.params.update(seconds=self.seconds, kind=self.kind)
        if posture:
            self.params.update(posture=posture)
        self._left = 0.0
        self._wait_left = 0.0
        self._fired = False

    def start(self, ctx: BehaviorContext) -> None:
        if self.posture:
            ctx.agent.posture = self.posture
            ctx.signal(
                WorldEvent("posture", ctx.agent.npc_id, {"posture": self.posture})
            )
        self._left = self.seconds
        self._fired = False
        self._wait_left = 0.0
        self.reason = ""
        # 清掉上一条动作留下的判决。不清的话，这条动作会立刻读到上一条的
        ctx.agent.blackboard.pop(ACTION_RESULT, None)
        if self.seconds <= 0:
            # 收不住的表演：没有"完成"可等，只能现在发（它不产生玩法结果）
            self._fire(ctx)

    def update(self, ctx: BehaviorContext) -> Status:
        if self.seconds <= 0:
            return Status.RUNNING          # 等外部打断

        if not self._fired:
            self._left -= ctx.dt
            if self._left > 0:
                return Status.RUNNING
            self._fire(ctx)                # 做完了才落地
            if not self.wait_result:
                return Status.SUCCESS
            # 挂起等判决。下一帧开始看黑板。
            self._wait_left = self.RESULT_TIMEOUT
            return Status.RUNNING

        got = ctx.agent.blackboard.pop(ACTION_RESULT, None)
        if got is not None:
            ok = bool(got[0]) if isinstance(got, (tuple, list)) else bool(got)
            msg = str(got[1]) if isinstance(got, (tuple, list)) and len(got) > 1 else ""
            if ok:
                return Status.SUCCESS
            self.reason = msg
            return Status.FAILURE

        self._wait_left -= ctx.dt
        if self._wait_left <= 0:
            # 没人来结算：当成功。行为树不许因为"上层没接"就卡住。
            return Status.SUCCESS
        return Status.RUNNING

    def _fire(self, ctx: BehaviorContext) -> None:
        if self._fired:
            return
        self._fired = True
        ctx.signal(
            WorldEvent(
                self.event,
                ctx.agent.npc_id,
                {"act": self.kind, **self.payload},
            )
        )

    def finish(self, ctx: BehaviorContext) -> None:
        if self.posture:
            ctx.agent.posture = "stand"
            ctx.signal(WorldEvent("posture", ctx.agent.npc_id, {"posture": "stand"}))

# ─────────────────────── 空行为（站着，什么都不做） ───────────────────────

class Idle(EmptyBehavior):
    op = "idle"

class Rest(EmptyBehavior):
    op = "rest"

    def start(self, ctx: BehaviorContext) -> None:
        ctx.agent.posture = "lie"
        ctx.signal(WorldEvent("posture", ctx.agent.npc_id, {"posture": "lie"}))

class AppreciateScenery(EmptyBehavior):
    """欣赏美景。目前是空实现，只是换个姿势站在那儿。"""

    op = "enjoy_view"

    def start(self, ctx: BehaviorContext) -> None:
        ctx.agent.posture = "lean"
        ctx.signal(WorldEvent("posture", ctx.agent.npc_id, {"posture": "lean"}))

DEFAULT_LEAVES = [
    MoveTo,
    Wander,
    Say,
    Invite,
    Offer,
    Deliver,
    Wait,
    Work,
    Idle,
    Rest,
    AppreciateScenery,
]
