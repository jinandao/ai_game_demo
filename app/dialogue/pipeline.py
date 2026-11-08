"""串联路由、取事实与语气层。"""

from __future__ import annotations

from typing import NamedTuple

from ..action import services
from ..action.gate import ActionGate
from ..domain.enums import ActionId, Intent, Stage, Subject
from ..domain.models import IntentResult, Persona, Profile, SessionContext
from ..memory.retriever import MemoryRetriever, is_step_log
from .handlers import DialogueRequest, IntentHandler
from .router import IntentRouter
from .stylist import Stylist

STAGE_LABEL = {
    Stage.STRANGER: "见过几面，不算熟",
    Stage.ACQUAINTANCE: "熟人",
    Stage.CLOSE: "亲近",
    Stage.AMBIGUOUS: "说不清",
    Stage.DEPENDENT: "离不开了",
}

SUBJECT_LABEL = {
    Subject.PLAYER: "关于对方",
    Subject.EVENT: "你们一起经历过",
    Subject.OTHER: "你知道的事",
}
PROMISE_LABEL = "你答应过的事"
# 闲聊时摆几条。3 条够了：再多她会以为"必须提一句"
CASUAL_TOPIC_K = 3

# 查资料 / 回忆时把历史压到 6 条。这类回答的重点是【本轮素材】
FOCUS_HISTORY = 6
FOCUS_INTENTS = {Intent.FACT_QUERY, Intent.RECALL}

ACTION_WHEN = {
    ActionId.INVITE_WALK: "聊得顺的时候，问问要不要一起在站里走一圈",
    ActionId.INVITE_VISIT: "对方情绪低落、或者外面太吵的时候，可以邀他来坐坐",
    ActionId.REQUEST_HUG: "对方明显难受、或者你们刚吵完又和好的时候，可以靠过去",
}

# 决策层写进深刻记忆的「互动」条目是步骤流水（见 memory/retriever.is_step_log）

class ActionRow(NamedTuple):
    """当前可提的一件事。"""

    id: str        # 按钮/标记用的 id
    label: str     # 给模型和按钮看的一句话
    when: str      # 什么时候会想到提这个
    kind: str      # serve（为他做）/ invite（提出一起做）

class DialoguePipeline:
    def __init__(
        self,
        router: IntentRouter,
        handlers: dict[Intent, IntentHandler],
        stylist: Stylist,
        retriever: MemoryRetriever | None = None,
        gate: ActionGate | None = None,
    ) -> None:
        self._router = router
        self._handlers = handlers
        self._stylist = stylist
        self._retriever = retriever
        self._gate = gate

        self.last_route: IntentResult | None = None   # 调试面板用
        self.last_memory: list[tuple[str, str]] = []
        self.last_topics: list[tuple[str, str]] = []  # 闲聊时可选的话题

    def respond(
        self,
        text: str,
        *,
        ctx: SessionContext,
        profile: Profile,
        persona: Persona,
        consent_granted: bool = False,
        pending_action: ActionId | None = None,
        mood: str = "",
        on_delta=None,
    ) -> str:
        route = self._router.route(text, ctx=ctx, profile=profile, persona=persona)
        self.last_route = route

        handler = self._handlers.get(route.intent) or self._handlers[Intent.CHITCHAT]
        req = DialogueRequest(
            text=text,
            ctx=ctx,
            profile=profile,
            persona=persona,
            consent_granted=consent_granted,
            pending_action=route.action_id or pending_action,
            scope=route.scope,
            query=route.query or text,
        )

        facts = handler.gather(req)   # None = 直通，不经过内容层

        # 记忆查不查由路由层决定。chitchat 时这里是 None
        need_memory = "memory" in route.need
        if need_memory:
            self.last_memory = self._memory_items(route.query or text, profile, persona)
            self.last_topics = []
        else:
            self.last_memory = []
            # 闲聊不检索（省一次 embed），但还是把几件"记着的事"摆在那儿
            self.last_topics = self._casual_topics(persona.npc_id)

        return self._stylist.render(
            facts,
            persona=persona,
            recent_turns=ctx.recent_turns,
            memory_items=self.last_memory if need_memory else None,
            topic_items=self.last_topics,
            unlocked_actions=self._unlocked_actions(profile, persona),
            stage=self._stage_of(profile, persona),
            ambience=ctx.ambience,
            outcome=ctx.outcome,
            summary=ctx.summary.text if ctx.summary else "",
            places=ctx.places,
            # 心情只影响她怎么说，不影响她说什么
            mood=mood,
            recent_limit=FOCUS_HISTORY if route.intent in FOCUS_INTENTS else None,
            on_delta=on_delta,
        )

    # ── 背景注入 ──

    def _memory_items(
        self, text: str, profile: Profile, persona: Persona
    ) -> list[tuple[str, str]]:
        """把玩家这句话相关的记忆取出来。"""
        if self._retriever is None:
            return []

        # 默认只查玩家画像 + 深刻记忆，动作流水不进来
        hits = self._retriever.retrieve(
            text,
            player_id=profile.player_id,
            npc_id=persona.npc_id,
            k=3,
        )
        items: list[tuple[str, str]] = []
        seen: set[str] = set()

        for hit in hits:
            subject = hit.meta.get("subject")
            if hit.meta.get("promise"):
                label = PROMISE_LABEL
            elif subject:
                try:
                    label = SUBJECT_LABEL.get(Subject(subject), "你记得")
                except ValueError:
                    label = "你记得"
            else:
                label = "你记得"
            if hit.text in seen:
                continue
            seen.add(hit.text)
            items.append((label, hit.text))

        return items

    def _casual_topics(self, npc_id: str) -> list[tuple[str, str]]:
        """闲聊时摆在手边的几件旧事。"""
        if self._retriever is None:
            return []
        picked: list[tuple[str, str]] = []
        # 多取一些再筛：深刻记忆里混着决策层写的「我为玩家做过：走到他身边
        for m in self._retriever.deep(npc_id, CASUAL_TOPIC_K * 3):
            if is_step_log(m.value):
                continue
            picked.append((SUBJECT_LABEL.get(m.subject, ""), m.value))
            if len(picked) >= CASUAL_TOPIC_K:
                break
        return picked

    def available_actions(
        self, profile: Profile, persona: Persona
    ) -> list[ActionRow]:
        """当前能提的全部举动。"""
        rows: list[ActionRow] = []
        if self._gate is not None:
            for action_id in self._gate.unlocked(profile, persona.npc_id):
                rows.append(
                    ActionRow(
                        id=action_id.value,
                        label=self._gate.describe(action_id),
                        when=ACTION_WHEN.get(action_id, "合适的时候可以提一句"),
                        kind="invite",
                    )
                )

        state = profile.affection.get(persona.npc_id)
        stage = state.stage if state else Stage.STRANGER
        for svc in services.for_stage(persona.npc_id, stage):
            rows.append(
                ActionRow(id=svc.id, label=svc.label, when=svc.when, kind="serve")
            )
        return rows

    def _unlocked_actions(
        self, profile: Profile, persona: Persona
    ) -> list[tuple[str, str, str]]:
        return [
            (row.label, row.when, row.kind)
            for row in self.available_actions(profile, persona)
        ]

    @staticmethod
    def _stage_of(profile: Profile, persona: Persona) -> Stage:
        state = profile.affection.get(persona.npc_id)
        return state.stage if state else Stage.STRANGER

    @staticmethod
    def _stage_label(profile: Profile, persona: Persona) -> str:
        return STAGE_LABEL.get(
            DialoguePipeline._stage_of(profile, persona), "刚认识"
        )
