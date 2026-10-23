"""动作域：三个邀请类动作。"""

from __future__ import annotations

from datetime import datetime

from ..domain.enums import ActionId, Stage, Subject
from ..domain.models import ActionContext, ActionResult, MemoryItem
from ..memory.store import new_memory_id
from .base import ActionTool


class _SessionAction(ActionTool):
    """三个动作的共同形状：写一条 event 记忆，返回一句中性描述。"""

    memory_key: str = ""

    def execute(self, ctx: ActionContext) -> ActionResult:
        if not ctx.consent_granted:
            return ActionResult(False, self.id, "还没得到同意")

        now = datetime.now()
        # 用时间戳做 pseudo range：动作记忆没有轮次概念，但幂等键要唯一，
        # 否则同一会话里第二次拥抱会被 store 当成重复条目跳过
        stamp = int(now.timestamp())
        item = MemoryItem(
            id=new_memory_id(),
            session_id=ctx.session_id,
            npc_id=ctx.npc_id,
            turn_range=(stamp, stamp),
            ts=now,
            subject=Subject.EVENT,
            key=self.memory_key,
            value=self.label,
            importance=4,
            evidence="",
            source="action",
        )
        return ActionResult(True, self.id, self.label, memory=item)


class InviteWalk(_SessionAction):
    id = ActionId.INVITE_WALK
    required_stage = Stage.ACQUAINTANCE
    label = "在站里走一圈"
    memory_key = "一起散步"


class InviteVisit(_SessionAction):
    id = ActionId.INVITE_VISIT
    required_stage = Stage.CLOSE
    label = "去她那儿坐坐"
    memory_key = "去过她那儿"


class RequestHug(_SessionAction):
    id = ActionId.REQUEST_HUG
    required_stage = Stage.AMBIGUOUS
    label = "她靠过来，抱了一下"
    memory_key = "拥抱"


def default_tools() -> dict[ActionId, ActionTool]:
    tools: list[ActionTool] = [InviteWalk(), InviteVisit(), RequestHug()]
    return {t.id: t for t in tools}
