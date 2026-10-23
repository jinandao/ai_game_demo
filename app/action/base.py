"""动作域：动作工具的抽象。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..domain.enums import ActionId, Stage
from ..domain.models import ActionContext, ActionResult


class ActionTool(ABC):
    id: ClassVar[ActionId]
    required_stage: ClassVar[Stage]
    requires_consent: ClassVar[bool] = True
    label: ClassVar[str] = ""

    @abstractmethod
    def execute(self, ctx: ActionContext) -> ActionResult:
        """执行。调用方必须已经过 ActionGate 且拿到玩家同意。"""

    def describe(self) -> str:
        return self.label
