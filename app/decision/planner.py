"""决策域：规划与校验。只输出意图，不生成台词。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from ..action import services
from ..domain.enums import DecisionIntent, Stage
from ..domain.models import Decision, NpcEvent, NpcState, Persona, Profile
from ..infra.clock import Clock
from ..infra.llm import LLMClient, LLMError
from ..memory.retriever import MemoryRetriever
from ..prompts.behavior import build_decision_system
from .rules import RuleEngine

# intent 之外还要 action：serve 必须说明"为他做哪件事"。
# **schema 里不写这个字段，模型就吐不出来**——它会严格按 schema 输出，
# 结果 serve 永远缺 action，全部落到 serve_invalid。
#
# 这个 schema 是按"这个 NPC、这个阶段"动态生成的（见 _schema_for），
# 不是全局的一份：否则 B 可能选到只属于 A 的动作，或者选了还没解锁的。
_BASE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["intent"],
}

# 决策只输出一个意图词，不需要理由。token 上限压到最低，
# 本地模型在"解释一下为什么"上浪费的时间比生成决定本身还长。
# 但别压太狠：推理模型（qwen3 等）即使关了 think，也可能多吐几个字符，
# 截断在 JSON 中间就整条报废。160 是"够一个 JSON + 冗余"的实测值。
SINGLE_MAX_TOKENS = 160
MAX_PARALLEL = 4

STAGE_CN = {
    "stranger": "陌生",
    "acquaintance": "熟人",
    "close": "亲近",
    "ambiguous": "暧昧",
    "dependent": "依赖",
}

# 光给一个单词，模型分不清 solo 和 work 的差别。
# 意图名是给代码看的，这里给的是给人（模型）看的一句话。
INTENT_HINT = {
    "idle": "待在原地发呆，什么也不做",
    "work": "干自己的活：走动、整理、守着摊位",
    "solo": "去个安静的地方待一会儿，看看风景或者想事情",
    "rest": "回自己房间躺下休息",
    "withdraw": "躲起来，谁也不想见",
    "seek_player": "主动去找玩家，走到他身边说话",
    "share_memory": "跟玩家提起一件你记着的事、或者你们之间的约定",
    "serve": "为他做点什么（具体做哪件看【可以为他做的事】，要选一个 id 填在 action 里）",
    "invite_walk": "问玩家要不要一起走走",
    "invite_visit": "邀玩家去你那儿坐坐",
    "request_hug": "跟玩家靠得近一点",
}


def _schema_for(npc_id: str, stage: Stage) -> dict:
    """按 NPC + 阶段生成 schema，action 只能取这个角色此刻真能做的那些。

    比全局枚举更严：模型不会选到别人的动作，也不会选到还没解锁的。
    """
    ids = [s.id for s in services.for_stage(npc_id, stage)]
    if not ids:
        return _BASE_SCHEMA
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "action": {"type": "string", "enum": [""] + ids},
        },
        "required": ["intent"],
    }


def _mood_cn(value: object) -> str:
    """mood 是 0~1 的浮点，直接丢给模型它只会看到「0.5」。
    翻译成它读得懂的，决策才会真的参考情绪。"""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "平静"
    if x < 0.3:
        return "很低落"
    if x < 0.5:
        return "有点闷"
    if x < 0.7:
        return "平静"
    return "心情不错"


class DecisionPlanner:
    def __init__(
        self,
        llm: LLMClient,
        rules: RuleEngine,
        retriever: MemoryRetriever,
        clock: Clock,
        personas: dict[str, Persona] | None = None,
        max_parallel: int = MAX_PARALLEL,
    ) -> None:
        self._llm = llm
        self._rules = rules
        self._retriever = retriever
        self._clock = clock
        self._personas = personas or {}
        self._max_parallel = max(1, max_parallel)
        self._failures = 0          # 模型调用/解析失败的累计次数，调试面板用
        self._last_error = ""       # 最近一次失败的原文，排查用
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_parallel, thread_name_prefix="decide-npc"
        )

    def decide(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        *,
        here: str = "",
        player_at: str = "",
    ) -> NpcEvent:
        allowed, recent_items, payload = self._context(
            npc_id, profile, state, here=here, player_at=player_at
        )
        affection = profile.affection.get(npc_id)
        stage = affection.stage if affection else Stage.STRANGER
        decision = self._ask(payload, npc_id, stage) or self._fallback(
            allowed, "decide_failed"
        )
        settled = self._settle(decision, state, allowed, recent_items)

        # serve 只做"走过去问一句"，不直接动手——执行要等玩家确认。
        if settled.intent is DecisionIntent.SERVE:
            svc_action = services.get(settled.action or "")
            if svc_action is None or svc_action.npc_id != npc_id:
                print(
                    f"[决策] {npc_id} serve 的 action 不合法: {settled.action!r}",
                    flush=True,
                )
                settled = self._fallback(allowed, "serve_invalid")
            else:
                settled = replace(settled, script=services.offer_script(svc_action.id))

        return NpcEvent(npc_id=npc_id, decision=settled)

    def decide_many(
        self, requests: dict[str, tuple[Profile, NpcState, str, str]]
    ) -> dict[str, NpcEvent]:
        """多个 NPC 同时决策，各自用完整提示词并发请求。能否真并行取决于 OLLAMA_NUM_PARALLEL。"""
        if not requests:
            return {}
        if len(requests) == 1:
            npc_id, (profile, state, here, player_at) = next(iter(requests.items()))
            return {
                npc_id: self.decide(npc_id, profile, state, here=here, player_at=player_at)
            }

        futures = {}
        for npc_id, (profile, state, here, player_at) in requests.items():
            futures[
                self._pool.submit(
                    self.decide, npc_id, profile, state, here=here, player_at=player_at
                )
            ] = npc_id

        out: dict[str, NpcEvent] = {}
        for future, npc_id in futures.items():
            try:
                out[npc_id] = future.result()
            except Exception:  # noqa: BLE001  单个失败不能拖垮整批
                self._failures += 1
                profile, state, here, player_at = requests[npc_id]
                allowed, _, _ = self._context(
                    npc_id, profile, state, here=here, player_at=player_at
                )
                out[npc_id] = NpcEvent(
                    npc_id=npc_id, decision=self._fallback(allowed, "decide_failed")
                )
        return out

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stats(self) -> dict[str, int]:
        return {"failures": self._failures}

    # ── 组装上下文 ──

    def _context(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        *,
        here: str = "",
        player_at: str = "",
    ):
        allowed = self._rules.allowed(
            profile=profile, npc_id=npc_id, state=state, now=self._clock.now()
        )
        unresolved = self._retriever.unresolved(profile.player_id, npc_id)
        recent_items = self._retriever.recent(npc_id, 8)
        # work / idle 是背景噪音：它们的记录会把模型锚定在"我一直在干活"上，
        # 越记越只会选 work。只把"有内容"的决策放进上下文。
        recent_lines = [
            m.value
            for m in recent_items
            if m.source == "decision" and m.value not in ("work", "idle")
        ]

        persona = self._personas.get(npc_id)
        affection = profile.affection.get(npc_id)
        stage = affection.stage if affection else Stage.STRANGER
        stage_cn = STAGE_CN.get(stage.value, "陌生")

        # 这个阶段能做的照料动作。一个都没有就把 serve 从可选意图里拿掉，
        # 免得模型选了一个不存在的动作，最后落到兜底。
        svc = services.for_stage(npc_id, stage)
        if not svc:
            allowed = allowed - {DecisionIntent.SERVE}

        payload = {
            "你是谁": persona.identity if persona else npc_id,
            "你们的关系": stage_cn,
            "现在": {
                "时间": self._clock.now().strftime("%H:%M"),
                "她在": here or "不知道",
                "玩家在": player_at or "不知道",
                "体力": round(float(state.stamina), 2),
                "情绪": _mood_cn(state.mood),
            },
            "允许的意图": [
                f"{i.value} —— {INTENT_HINT.get(i.value, '')}"
                for i in sorted(allowed, key=lambda x: x.value)
            ],
            "可以为他做的事": [
                f"{s.id} —— {s.label}（{s.when}）" for s in svc
            ],
            "待办约定": [m.value for m in unresolved],
            "最近做过的事": recent_lines,
        }
        return allowed, recent_items, payload

    def _settle(
        self,
        decision: Decision,
        state: NpcState,
        allowed: set[DecisionIntent],
        recent_items: list,
    ) -> Decision:
        validated = DecisionValidator.validate(decision, allowed)
        if validated is None:
            validated = self._fallback(allowed, "not_allowed")
        return self._avoid_repeat(validated, state, allowed, recent_items)

    # 这些意图连续出现是正常的：干活本来就该连着干；
    # withdraw 是规则强制的（体力低），也不能被"换一件"改掉。
    # **idle 不在这里**：连续发呆会被强制改成 work，否则两个人一起站着不动。
    KEEP_REPEAT = {DecisionIntent.WORK, DecisionIntent.WITHDRAW}

    def _avoid_repeat(
        self,
        decision: Decision,
        state: NpcState,
        allowed: set[DecisionIntent],
        recent_items: list,
    ) -> Decision:
        """刚做过的事不再做一遍。上一次决定优先从 NpcState 读，读不到则从决策记忆反解。"""
        last_intent = state.last_decision.intent if state.last_decision else None
        if last_intent is None:
            for m in recent_items:
                if m.source == "decision" and m.key.startswith("决策_"):
                    try:
                        last_intent = DecisionIntent(m.key[len("决策_"):])
                    except ValueError:
                        continue
                    break

        if last_intent is None or decision.intent != last_intent:
            return decision
        if decision.intent in self.KEEP_REPEAT:
            return decision

        fallback = (
            DecisionIntent.WORK
            if DecisionIntent.WORK in allowed
            else DecisionIntent.IDLE
        )
        return Decision(
            intent=fallback,
            reason=f"刚做过{decision.intent.value}，换一件",
            urgency=0.3,
            script=None,
        )

    def _ask(self, payload: dict, npc_id: str = "", stage: Stage = Stage.STRANGER) -> Decision | None:
        messages = [
            {"role": "system", "content": build_decision_system()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            raw = self._llm.complete_json(
                messages,
                schema=_schema_for(npc_id, stage),
                max_tokens=SINGLE_MAX_TOKENS,
            )
        except LLMError as e:
            self._failures += 1
            self._last_error = str(e)[:160]
            print(f"[决策失败] {npc_id}: {e}", flush=True)
            return None
        decision = self._parse(raw)
        if decision is None:
            self._failures += 1
            self._last_error = f"无法解析 intent: {str(raw)[:120]}"
            print(f"[决策失败] {npc_id}: {self._last_error}", flush=True)
        return decision

    def _fallback(self, allowed: set[DecisionIntent], why: str) -> Decision:
        """决策失败兜底：优先 work，其次 solo，避免退到 idle 让 NPC 静止。"""
        for candidate in (DecisionIntent.WORK, DecisionIntent.SOLO):
            if candidate in allowed:
                return Decision(intent=candidate, reason=why, urgency=0.3)
        return Decision(intent=DecisionIntent.IDLE, reason=why, urgency=0.3)

    def _parse(self, raw: dict | None) -> Decision | None:
        """只取 intent 与 action；具体流程由 director 的模板翻译。"""
        if not isinstance(raw, dict):
            return None
        try:
            intent = DecisionIntent(raw["intent"])
        except (KeyError, ValueError, TypeError):
            return None
        action = raw.get("action")
        return Decision(
            intent=intent,
            reason="",
            urgency=0.5,
            action=str(action) if action else None,
        )


class DecisionValidator:
    @staticmethod
    def validate(d: Decision, allowed: set[DecisionIntent]) -> Decision | None:
        """白名单外的直接丢弃。LLM 越界的唯一处理方式是不给它机会生效。"""
        if d.intent not in allowed:
            return None
        return d
