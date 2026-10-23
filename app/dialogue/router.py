"""对话域：意图路由，解析失败降级为闲聊。"""

from __future__ import annotations

from ..domain.enums import ActionId, Intent, Scope
from ..domain.models import IntentResult, Persona, Profile, SessionContext
from ..infra.llm import LLMClient, LLMError
from ..prompts.routing import ROUTER_SYSTEM, build_router_user

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"enum": [i.value for i in Intent]},
        "scope": {"enum": [s.value for s in Scope]},
        "action_id": {"enum": [a.value for a in ActionId]},
        "need": {"type": "array", "items": {"enum": ["kb", "memory"]}},
        "query": {"type": "string"},
    },
    "required": ["intent"],
}

# intent 和 need 的固定搭配。模型偶尔只给 intent，这里补齐。
NEED_BY_INTENT = {
    Intent.FACT_QUERY: ("kb",),
    Intent.RECALL: ("memory",),
}


class IntentRouter:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    def route(
        self, text: str, *, ctx: SessionContext, profile: Profile, persona: Persona
    ) -> IntentResult:
        # 8 条约等于 4 轮；指代消解需要更长的上下文。
        # router 的提示词很短，多给几条成本几乎为零。
        recent = "\n".join(f"{t.role}：{t.text}" for t in ctx.recent_turns[-8:])
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": build_router_user(recent, text)},
        ]
        try:
            raw = self._llm.complete_json(messages, schema=SCHEMA)
        except LLMError:
            return IntentResult(intent=Intent.CHITCHAT)

        return self._validate(raw)

    @staticmethod
    def _validate(raw: dict) -> IntentResult:
        try:
            intent = Intent(raw["intent"])
        except (KeyError, ValueError):
            intent = Intent.CHITCHAT

        scope = None
        if raw.get("scope"):
            try:
                scope = Scope(raw["scope"])
            except ValueError:
                scope = Scope.WORLD if intent is Intent.FACT_QUERY else None
        elif intent is Intent.FACT_QUERY:
            scope = Scope.WORLD

        action_id = None
        if intent is Intent.ACTION and raw.get("action_id"):
            try:
                action_id = ActionId(raw["action_id"])
            except ValueError:
                action_id = None

        # need 由模型决定；它漏给时按 intent 补，绝不在 chitchat 上瞎补
        raw_need = raw.get("need")
        if isinstance(raw_need, list):
            need = tuple(n for n in raw_need if n in ("kb", "memory"))
        else:
            need = NEED_BY_INTENT.get(intent, ())

        query = str(raw.get("query") or "").strip()[:80]

        return IntentResult(
            intent=intent, scope=scope, action_id=action_id, need=need, query=query
        )
