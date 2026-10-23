"""行为域：叶子行为基类。

支持继承重写，也支持给实例赋值 handler / on_start / on_finish 做运行时改写。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from .context import BehaviorContext
from .node import Node, Status

Handler = Callable[[BehaviorContext, "Behavior"], Status]
Hook = Callable[[BehaviorContext, "Behavior"], None]


class Behavior(Node):
    op: ClassVar[str] = "behavior"

    def __init__(
        self,
        *,
        handler: Handler | None = None,
        on_start: Hook | None = None,
        on_finish: Hook | None = None,
        **params: Any,
    ) -> None:
        self.handler = handler
        self.on_start = on_start
        self.on_finish = on_finish
        self.params: dict[str, Any] = dict(params)
        for k, v in params.items():
            setattr(self, k, v)      # 成员赋值即可改参数：b.duration = 5

        self.status = Status.RUNNING
        self._started = False
        self._done = False

    # ── 执行骨架（不要重写） ──

    def tick(self, ctx: BehaviorContext) -> Status:
        if self._done:
            return self.status

        if not self._started:
            self._started = True
            self.start(ctx)
            if self.on_start:
                self.on_start(ctx, self)

        st = self.handler(ctx, self) if self.handler else self.update(ctx)
        self.status = st if isinstance(st, Status) else Status.RUNNING

        if self.status is not Status.RUNNING:
            self.finish(ctx)
            if self.on_finish:
                self.on_finish(ctx, self)
            self._done = True
        return self.status

    def reset(self) -> None:
        self._started = False
        self._done = False
        self.status = Status.RUNNING

    # ── 子类钩子 ──

    def start(self, ctx: BehaviorContext) -> None:
        """第一次 tick 前调用一次。定位目标、初始化计时器放这儿。"""

    def update(self, ctx: BehaviorContext) -> Status:
        """每 tick 调用。默认立刻成功——空行为要"一直站着"就重写它。"""
        return Status.SUCCESS

    def finish(self, ctx: BehaviorContext) -> None:
        """结束（成功或失败）后调用一次。清理 target / posture 放这儿。"""

    # ── 展示 ──

    def to_dsl(self, level: int = 0) -> str:
        pad = " " * (level * 2)
        if not self.params:
            return f"{pad}{self.op}"
        args = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{pad}{self.op}({args})"


class EmptyBehavior(Behavior):
    """空行为，永远 RUNNING。不能放在 seq 中间，否则后面的节点不会执行。"""

    def start(self, ctx: BehaviorContext) -> None:
        ctx.agent.posture = "stand"

    def update(self, ctx: BehaviorContext) -> Status:
        return Status.RUNNING
