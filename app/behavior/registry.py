"""行为域：op 名到实现的注册表。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .behavior import Behavior
from .library import DEFAULT_LEAVES
from .node import Node

Factory = Callable[..., Node]


class BehaviorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Factory] = {}

    # ── 注册 ──

    def register(self, op: str, factory: Factory) -> "BehaviorRegistry":
        """注册一个类或工厂函数。工厂会被 **params 调用。"""
        self._factories[op] = factory
        return self

    def register_behavior(self, cls: type[Behavior]) -> "BehaviorRegistry":
        """注册一个带 op 的 Behavior 子类。"""
        if not cls.op:
            raise ValueError(f"{cls.__name__} 没有 op")
        return self.register(cls.op, cls)

    def alias(self, name: str, op: str) -> "BehaviorRegistry":
        if op in self._factories:
            self._factories[name] = self._factories[op]
        return self

    # ── 查询 ──

    def has(self, op: str) -> bool:
        return op in self._factories

    def ops(self) -> list[str]:
        return sorted(self._factories)

    def create(self, op: str, params: dict[str, Any]) -> Node | None:
        """未知 op 返回 None —— 由 parser 决定怎么降级，这里不抛。"""
        factory = self._factories.get(op)
        if factory is None:
            return None
        return factory(**params)


def default_registry() -> BehaviorRegistry:
    reg = BehaviorRegistry()
    for cls in DEFAULT_LEAVES:
        reg.register_behavior(cls)
    reg.alias("talk", "say")
    reg.alias("walk", "move")
    return reg
