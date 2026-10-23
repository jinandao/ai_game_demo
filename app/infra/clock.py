"""基础设施：时钟。好感度衰减、冷却、检索新近度都依赖它，必须可注入。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now()


class FrozenClock(Clock):
    """测试与演示用：advance 手动推进，让"跨天衰减""冷却"可复现。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now()

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now
