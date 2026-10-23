"""对话域：五个意图处理器。

gather() 只负责取事实，返回 None 表示直通语气层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ..action.base import ActionTool
from ..action.gate import ActionGate
from ..domain.enums import ActionId, Intent, Scope
from ..domain.models import (
    ActionContext,
    Facts,
    Persona,
    Profile,
    SessionContext,
)
from ..knowledge.base import KnowledgeBase
from ..memory.retriever import MemoryRetriever
from .composer import Composer


@dataclass(frozen=True)
class DialogueRequest:
    text: str
    ctx: SessionContext
    profile: Profile
    persona: Persona
    consent_granted: bool = False
    pending_action: ActionId | None = None
    scope: Scope | None = None
    query: str = ""          # 路由层给的检索词；空则退回原文


class IntentHandler(ABC):
    intent: ClassVar[Intent]

    @abstractmethod
    def gather(self, req: DialogueRequest) -> Facts | None: ...


class ChitchatHandler(IntentHandler):
    """闲聊不产出事实，直接交给语气层。"""

    intent = Intent.CHITCHAT

    def gather(self, req: DialogueRequest) -> None:
        return None


class OutOfScopeHandler(IntentHandler):
    """越界给中性事实，不给禁令；措辞由语气层决定。"""

    intent = Intent.OUT_OF_SCOPE

    def gather(self, req: DialogueRequest) -> Facts:
        return Facts.of(
            [
                "这个问题超出她的认知范围",
                "她可以不做解释地带过，或者反问一句，不要像在背规章",
                "拒完要把话拉回眼前的人和事",
            ],
            kind="out_of_scope",
        )


class FactQueryHandler(IntentHandler):
    """世界观 / 角色背景。

    低于阈值直接返回空——防幻觉靠"没把握时检索返回空"，不靠提示词叮嘱。
    """

    intent = Intent.FACT_QUERY

    def __init__(self, kb: KnowledgeBase, composer: Composer, floor: float = 0.0) -> None:
        self._kb = kb
        self._composer = composer
        self._floor = floor

    def gather(self, req: DialogueRequest) -> Facts:
        scope = req.scope or Scope.WORLD
        # 用模型改写过的检索词，比原句更贴近资料里的说法（"今天不开门吗" → "闸门开放时间"）
        query = req.query or req.text
        hits = self._kb.retrieve(query, scope=scope, npc_id=req.persona.npc_id)

        # 分类可能偏差：scope 查不到时回落 world 重查。
        if not hits and scope is not Scope.WORLD:
            hits = self._kb.retrieve(query, scope=Scope.WORLD, npc_id=req.persona.npc_id)

        if not hits or hits[0].score < self._floor:
            return Facts.empty(confidence=hits[0].score if hits else 0.0)
        return self._composer.compose(hits, intent=self.intent)


class RecallHandler(IntentHandler):
    """玩家自身信息。记忆由 pipeline 注入，此处不重复检索。"""

    intent = Intent.RECALL

    def __init__(self, retriever: MemoryRetriever, composer: Composer) -> None:
        self._retriever = retriever
        self._composer = composer

    def gather(self, req: DialogueRequest) -> Facts | None:
        return None


class ActionHandler(IntentHandler):
    """动作。闸门判定在这里，执行不在——执行必须等玩家明确同意。"""

    intent = Intent.ACTION

    def __init__(
        self,
        gate: ActionGate,
        tools: dict[ActionId, ActionTool],
        composer: Composer,
        on_action=None,
    ) -> None:
        self._gate = gate
        self._tools = tools
        self._composer = composer
        self._on_action = on_action

    def gather(self, req: DialogueRequest) -> Facts:
        action_id = req.pending_action
        if action_id is None:
            return Facts.of(["玩家表达了想一起做点什么的意愿"], kind="normal")

        tool = self._tools.get(action_id)
        if tool is None:
            return Facts.of(["这个动作不存在"], kind="action_denied")

        gate = self._gate.check(action_id, req.profile, req.persona.npc_id)
        if not gate.allowed:
            return Facts.of([f"现在还不能{action_id.value}：{gate.reason}"], kind="action_denied")

        if req.consent_granted:
            ctx = ActionContext(
                player=req.profile,
                npc_id=req.persona.npc_id,
                session_id=req.ctx.session_id,
                consent_granted=True,
            )
            result = tool.execute(ctx)
            if result.ok:
                # 动作产生的事件记忆必须落盘，否则她下次就不记得一起做过什么
                if result.memory is not None and self._on_action is not None:
                    self._on_action(result.memory)
                return Facts.of([result.detail], kind="normal")
            return Facts.of([f"没能成行：{result.detail}"], kind="action_denied")

        return Facts.of(
            [f"她想{action_id.value}，但必须先问玩家同不同意；现在还没问过"],
            kind="action_proposal",
        )


def default_handlers(
    *,
    kb: KnowledgeBase,
    retriever: MemoryRetriever,
    gate: ActionGate,
    tools: dict[ActionId, ActionTool],
    composer: Composer,
    sim_floor: float = 0.0,
    on_action=None,
) -> dict[Intent, IntentHandler]:
    handlers: list[IntentHandler] = [
        ChitchatHandler(),
        OutOfScopeHandler(),
        FactQueryHandler(kb, composer, floor=sim_floor),
        RecallHandler(retriever, composer),
        ActionHandler(gate, tools, composer, on_action=on_action),
    ]
    return {h.intent: h for h in handlers}
