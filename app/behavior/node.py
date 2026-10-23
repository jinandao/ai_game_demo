"""行为域：行为树节点。

四种组合子（seq / sel / repeat / parallel），三种状态。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Iterator

from .context import BehaviorContext


class Status(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"


class Node(ABC):
    op: str = "node"

    @abstractmethod
    def tick(self, ctx: BehaviorContext) -> Status: ...

    def reset(self) -> None:
        """中断或重跑时重置内部游标。组合子必须递归重置。"""

    def leaves(self) -> Iterator["Behavior"]:  # noqa: F821  延迟引用
        return iter(())

    def to_dsl(self, level: int = 0) -> str:
        return " " * (level * 2) + self.op


class Sequence(Node):
    op = "seq"

    def __init__(self, children: list[Node]) -> None:
        self.children = list(children)
        self._i = 0

    def tick(self, ctx: BehaviorContext) -> Status:
        while self._i < len(self.children):
            st = self.children[self._i].tick(ctx)
            if st is Status.RUNNING:
                return Status.RUNNING
            if st is Status.FAILURE:
                self.reset()
                return Status.FAILURE
            self._i += 1
        self.reset()
        return Status.SUCCESS

    def reset(self) -> None:
        self._i = 0
        for c in self.children:
            c.reset()

    def leaves(self) -> Iterator["Behavior"]:
        for c in self.children:
            yield from c.leaves()

    def to_dsl(self, level: int = 0) -> str:
        pad = " " * (level * 2)
        body = "\n".join(c.to_dsl(level + 1) for c in self.children)
        return f"{pad}seq {{\n{body}\n{pad}}}"


class Selector(Node):
    op = "sel"

    def __init__(self, children: list[Node]) -> None:
        self.children = list(children)
        self._i = 0

    def tick(self, ctx: BehaviorContext) -> Status:
        while self._i < len(self.children):
            st = self.children[self._i].tick(ctx)
            if st is Status.RUNNING:
                return Status.RUNNING
            if st is Status.SUCCESS:
                self.reset()
                return Status.SUCCESS
            self._i += 1
        self.reset()
        return Status.FAILURE

    def reset(self) -> None:
        self._i = 0
        for c in self.children:
            c.reset()

    def leaves(self) -> Iterator["Behavior"]:
        for c in self.children:
            yield from c.leaves()

    def to_dsl(self, level: int = 0) -> str:
        pad = " " * (level * 2)
        body = "\n".join(c.to_dsl(level + 1) for c in self.children)
        return f"{pad}sel {{\n{body}\n{pad}}}"


class Repeat(Node):
    op = "repeat"

    def __init__(self, child: Node, times: int = -1) -> None:
        self.child = child
        self.times = times
        self._done = 0

    def tick(self, ctx: BehaviorContext) -> Status:
        while self.times < 0 or self._done < self.times:
            st = self.child.tick(ctx)
            if st is Status.RUNNING:
                return Status.RUNNING
            if st is Status.FAILURE:
                self.reset()
                return Status.FAILURE
            self._done += 1
        self.reset()
        return Status.SUCCESS

    def reset(self) -> None:
        self._done = 0
        self.child.reset()

    def leaves(self) -> Iterator["Behavior"]:
        yield from self.child.leaves()

    def to_dsl(self, level: int = 0) -> str:
        pad = " " * (level * 2)
        n = "inf" if self.times < 0 else str(self.times)
        return f"{pad}repeat({n}) {{\n{self.child.to_dsl(level + 1)}\n{pad}}}"


class Parallel(Node):
    op = "parallel"

    def __init__(self, children: list[Node]) -> None:
        self.children = list(children)

    def tick(self, ctx: BehaviorContext) -> Status:
        results = [c.tick(ctx) for c in self.children]
        if any(r is Status.FAILURE for r in results):
            self.reset()
            return Status.FAILURE
        if all(r is Status.SUCCESS for r in results):
            self.reset()
            return Status.SUCCESS
        return Status.RUNNING

    def reset(self) -> None:
        for c in self.children:
            c.reset()

    def leaves(self) -> Iterator["Behavior"]:
        for c in self.children:
            yield from c.leaves()



    def to_dsl(self, level: int = 0) -> str:
        pad = " " * (level * 2)
        body = "\n".join(c.to_dsl(level + 1) for c in self.children)
        return f"{pad}par {{\n{body}\n{pad}}}"
