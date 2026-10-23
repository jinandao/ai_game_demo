"""动作域：阶段闸门。

只检查当前阶段是否解锁；是否需要玩家同意由调用方处理。
"""

from __future__ import annotations

from ..domain.enums import ActionId, Stage
from ..domain.models import GateResult, Profile
from ..relation.affection import AffectionEngine
from .base import ActionTool

STAGE_ORDER: list[Stage] = [
    Stage.STRANGER,
    Stage.ACQUAINTANCE,
    Stage.CLOSE,
    Stage.AMBIGUOUS,
    Stage.DEPENDENT,
]


class ActionGate:
    def __init__(self, tools: dict[ActionId, ActionTool], affection: AffectionEngine) -> None:
        self._tools = tools
        self._affection = affection

    def check(self, action_id: ActionId, profile: Profile, npc_id: str) -> GateResult:
        tool = self._tools.get(action_id)
        if tool is None:
            return GateResult(False, "没有这个动作", False, Stage.STRANGER)

        current = self._affection.state_of(profile, npc_id).stage
        if STAGE_ORDER.index(current) < STAGE_ORDER.index(tool.required_stage):
            return GateResult(
                allowed=False,
                reason=f"还没到{tool.required_stage.value}这一步",
                needs_consent=True,
                required_stage=tool.required_stage,
            )

        return GateResult(
            allowed=True,
            reason="",
            needs_consent=tool.requires_consent,
            required_stage=tool.required_stage,
        )

    def unlocked(self, profile: Profile, npc_id: str) -> list[ActionId]:
        """UI 用：当前阶段能显示哪些动作按钮。"""
        return [aid for aid in self._tools if self.check(aid, profile, npc_id).allowed]

    def describe(self, action_id: ActionId) -> str:
        """给提示词用的一句话动作名，避免把工具实例的其它细节漏到对话层。"""
        tool = self._tools.get(action_id)
        return tool.describe() if tool else action_id.value
