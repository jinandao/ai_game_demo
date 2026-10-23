"""记忆域：抽取器，压缩与会话结束时共用。"""

from __future__ import annotations

from datetime import datetime

from ..domain.enums import ExtractMode, Subject
from ..domain.models import (
    AffectionHint,
    ExtractResult,
    MemoryItem,
    SessionSummary,
    Turn,
)
from ..infra.clock import Clock
from ..infra.config import Config
from ..infra.llm import LLMClient, LLMError
from ..prompts.memory import build_extract_system, build_extract_user
from .store import new_memory_id

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"enum": [s.value for s in Subject]},
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "evidence": {"type": "string"},
                    "promise": {"type": "boolean"},
                },
                "required": ["subject", "key", "value", "importance"],
            },
        },
        "affection_hint": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"delta": {"type": "integer"}, "reason": {"type": "string"}},
            },
        },
    },
    "required": ["summary", "facts"],
}

class Extractor:
    def __init__(self, llm: LLMClient, cfg: Config, clock: Clock) -> None:
        self._llm = llm
        self._cfg = cfg
        self._clock = clock

    def extract(
        self,
        turns: list[Turn],
        *,
        npc_id: str,
        session_id: str,
        last_summary: SessionSummary | None = None,
        mode: ExtractMode = ExtractMode.FINAL,
    ) -> ExtractResult:
        if not turns:
            return ExtractResult(summary=None)

        turn_range = (turns[0].idx, turns[-1].idx)
        try:
            raw = self._llm.complete_json(
                self._build_messages(turns, last_summary, mode),
                schema=SCHEMA,
                # 摘要 300 字 + 若干条事实。上限放宽到 900：
                # 事实多的时候 700 会把数组截断，截断的 JSON 整条报废，
                # 结果比"少抽几条"更糟。
                max_tokens=900,
            )
        except LLMError:
            return self._fallback(turns, npc_id, session_id, turn_range)

        return self._parse(raw, turns, npc_id, session_id, turn_range)

    # ── 内部 ──

    def _build_messages(self, turns, last_summary, mode) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": build_extract_system(mode.value)},
            {
                "role": "user",
                "content": build_extract_user(
                    render_turns(turns),
                    last_summary.text if last_summary else None,
                ),
            },
        ]

    def _parse(self, raw, turns, npc_id, session_id, turn_range) -> ExtractResult:
        summary_text = (raw.get("summary") or "").strip()[: self._cfg.summary_max_chars] or None
        ts = self._clock.now()

        facts: list[MemoryItem] = []
        for row in raw.get("facts", []) or []:
            try:
                subject = Subject(row["subject"])
            except (KeyError, ValueError):
                continue
            key = (row.get("key") or "").strip()
            value = (row.get("value") or "").strip()
            if not key or not value:
                continue
            facts.append(
                MemoryItem(
                    id=new_memory_id(),
                    session_id=session_id,
                    npc_id=npc_id,
                    turn_range=turn_range,
                    ts=ts,
                    subject=subject,
                    key=key[:32],
                    value=value[:300],
                    importance=max(1, min(5, int(row.get("importance", 3)))),
                    evidence=(row.get("evidence") or "")[:200],
                    source="dialogue",
                    promise=bool(row.get("promise", False)),
                )
            )

        hints = tuple(
            AffectionHint(delta=int(h.get("delta", 0)), reason=h.get("reason", ""))
            for h in (raw.get("affection_hint") or [])
            if int(h.get("delta", 0)) != 0
        )

        return ExtractResult(summary=summary_text, facts=tuple(facts), hints=hints)

    def _fallback(self, turns, npc_id, session_id, turn_range) -> ExtractResult:
        """JSON 炸了也要留住东西。宁可脏，不能丢。"""
        text = render_turns(turns)[:500]
        return ExtractResult(
            summary=None,
            facts=(
                MemoryItem(
                    id=new_memory_id(),
                    session_id=session_id,
                    npc_id=npc_id,
                    turn_range=turn_range,
                    ts=self._clock.now(),
                    subject=Subject.OTHER,
                    key="未整理对话",
                    value=text,
                    importance=2,
                    evidence="",
                    source="fallback",
                    dedupe=False,      # 兜底原文每次都留，不要互相覆盖
                ),
            ),
            degraded=True,
        )


def render_turns(turns: list[Turn], npc_name: str = "她") -> str:
    lines = []
    for t in turns:
        who = "玩家" if t.role == "player" else npc_name
        lines.append(f"{who}：{t.text}")
    return "\n".join(lines)
