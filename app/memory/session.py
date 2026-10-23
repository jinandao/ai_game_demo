"""记忆域：ChatSession，会话门面。

独占三件事：L0/L1 生命周期、抽取时机、把抽取产物交给 store 与好感度。
turns 全量保留，只记录覆盖到哪一轮，保证 idx 连续。
"""

from __future__ import annotations

from ..domain.enums import ActionId, ExtractMode
from ..domain.models import (
    EndReport,
    Persona,
    Profile,
    SessionContext,
    SessionSummary,
    Turn,
)
from ..dialogue.pipeline import DialoguePipeline
from ..infra.clock import Clock
from ..infra.config import Config
from ..infra.storage import JsonFile
from ..relation.affection import AffectionEngine
from .compactor import Compactor, estimate_tokens
from .extractor import Extractor, render_turns
from .store import MemoryStore


class SessionClosed(RuntimeError):
    pass


class ChatSession:
    def __init__(
        self,
        *,
        session_id: str,
        player: Profile,
        persona: Persona,
        pipeline: DialoguePipeline,
        compactor: Compactor,
        extractor: Extractor,
        store: MemoryStore,
        affection: AffectionEngine,
        cfg: Config,
        clock: Clock,
        ambience: str = "",
        outcome: str = "",
    ) -> None:
        self.session_id = session_id
        self.player = player
        self.persona = persona
        self.npc_id = persona.npc_id
        self.ambience = ambience      # 当前场景的客观环境，注入提示词防止天气乱编
        self.outcome = outcome        # 她刚为他做的事，写进「刚刚发生」

        self.turns: list[Turn] = []          # L0，全量保留
        self.summary: SessionSummary | None = None  # L1
        self.is_closed = False

        self._pipeline = pipeline
        self._compactor = compactor
        self._extractor = extractor
        self._store = store
        self._affection = affection
        self._cfg = cfg
        self._clock = clock

        # 覆盖水位（含），-1 表示还没覆盖任何一轮
        self._summarized_until: int = -1
        self._extracted_until: int = -1

        self._pending_action: ActionId | None = None
        self._consent: bool = False

    # ─────────────────────── 对外 ───────────────────────

    def preload_npc_turn(self, text: str) -> Turn | None:
        """把 NPC 主动开口的那一句写进 L0，让她的第一次回复能接上自己刚说的话。"""
        if self.is_closed or not text.strip():
            return None
        return self._append("npc", text.strip())

    def send(self, text: str, on_delta=None) -> Turn:
        if self.is_closed:
            raise SessionClosed("会话已结束，请重新 enter_chat")

        self._append("player", text)
        ctx = self._context()

        reply = self._pipeline.respond(
            text,
            ctx=ctx,
            profile=self.player,
            persona=self.persona,
            consent_granted=self._consent,
            pending_action=self._pending_action,
            on_delta=on_delta,
        )
        npc_turn = self._append("npc", reply)

        self._consent = False  # 同意是一次性的，用完即失效
        self._maybe_compact()
        return npc_turn

    def grant_consent(self, action_id: ActionId) -> None:
        """玩家点了"好啊"。下一轮由 ActionHandler 消费。"""
        self._pending_action = action_id
        self._consent = True

    def end(self) -> EndReport:
        """结束。先抽后毁，顺序不能反。"""
        if self.is_closed:
            return EndReport(self.session_id, 0, 0, 0, False)

        total_turns = len(self.turns)
        pending = self.turns[self._extracted_until + 1 :]

        result = self._extractor.extract(
            pending,
            npc_id=self.npc_id,
            session_id=self.session_id,
            mode=ExtractMode.FINAL,
        )
        written = self._store.append(list(result.facts))
        delta = self._affection.apply_hints(
            self.player, self.npc_id, list(result.hints)
        )

        self.player.last_session_at = self._clock.now()
        report = EndReport(
            session_id=self.session_id,
            turns=total_turns,
            facts_written=written,
            affection_delta=delta,
            degraded=result.degraded,
        )
        self._destroy()
        return report

    def transcript(self) -> list[Turn]:
        return list(self.turns)

    def stats(self) -> dict[str, object]:
        """会话统计，供调试面板使用。"""
        return {
            "session_id": self.session_id,
            "turns": len(self.turns),
            "est_tokens": estimate_tokens(self.turns, self.summary),
            "t_compact": self._cfg.t_compact,
            "compact_ratio": self._cfg.compact_ratio,
            "summary_level": self.summary.level if self.summary else None,
            "summary_text": self.summary.text if self.summary else None,
            "summarized_until": self._summarized_until,
            "extracted_until": self._extracted_until,
        }

    # ─────────────────────── 内部 ───────────────────────

    def _append(self, role: str, text: str) -> Turn:
        turn = Turn(
            idx=len(self.turns),
            role=role,  # type: ignore[arg-type]
            text=text,
            ts=self._clock.now(),
        )
        self.turns.append(turn)
        return turn

    def _context(self) -> SessionContext:
        return SessionContext(
            session_id=self.session_id,
            npc_id=self.npc_id,
            summary=self.summary,
            recent_turns=tuple(self.turns[-self._cfg.compact_window_turns:]),
            turn_count=len(self.turns),
            ambience=self.ambience,
            outcome=self.outcome,
        )

    def _maybe_compact(self) -> None:
        window = self.turns[self._summarized_until + 1 :]
        if not self._compactor.should(window, self.summary):
            return

        result = self._compactor.compact(
            window, self.summary, npc_id=self.npc_id, session_id=self.session_id
        )
        if result is None:
            return

        self.summary = result.summary
        self._summarized_until = result.consumed_upto
        self._extracted_until = max(self._extracted_until, result.consumed_upto)
        self._store.append(list(result.facts))

    def _destroy(self) -> None:
        self.turns.clear()
        self.summary = None
        self._summarized_until = -1
        self._extracted_until = -1
        self._pending_action = None
        self._consent = False
        self.is_closed = True
