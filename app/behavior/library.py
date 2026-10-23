"""行为域：标准行为库，按 op 名注册到 BehaviorRegistry。"""

from __future__ import annotations

import math
import random

from ..domain.enums import ActionId
from .behavior import Behavior, EmptyBehavior
from .context import BehaviorContext, SpeakRequest, WorldEvent
from .node import Status


class MoveTo(Behavior):
    """移动到场景内坐标或跨场景移动。place 支持场景 id、$player、?；跨场景走最短路。"""

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

        # 走到某个道具旁边：灶台、工作台、铁柜……
        # 落在道具下沿外侧，看起来像"站在它前面"，而不是踩在它身上。
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
    """在当前场景内随机走一小段，不跨场景。"""

    op = "wander"
    X_MIN, X_MAX = 180.0, 1260.0
    Y_MIN, Y_MAX = 340.0, 740.0

    def __init__(self, radius: float = 220.0, **kw) -> None:
        super().__init__(**kw)
        self.radius = radius
        self.params.update(radius=radius)
        self._move: MoveTo | None = None

    def start(self, ctx: BehaviorContext) -> None:
        ax, ay = ctx.agent.x, ctx.agent.y
        x = ax + random.uniform(-self.radius, self.radius)
        y = ay + random.uniform(-self.radius * 0.5, self.radius * 0.5)
        x = max(self.X_MIN, min(self.X_MAX, x))
        y = max(self.Y_MIN, min(self.Y_MAX, y))

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
    """发起邀请。await_consent=True 时一直 RUNNING，直到 blackboard 写入答复。"""

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
    """提议为玩家做一件事，只问不做。执行要等玩家在对话里确认。"""

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
    """交付刚做完的事，并把 outcome 挂到事件上供后续对话引用。"""

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


# ── 空行为（永远 RUNNING） ──


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
    Idle,
    Rest,
    AppreciateScenery,
]
