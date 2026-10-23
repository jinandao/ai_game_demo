"""记忆域：压缩器。

尾部 k_tail_turns 轮不参与压缩；每次只压前文的 compact_ratio，入参为旧摘要加本段原文。
"""

from __future__ import annotations

from ..domain.enums import ExtractMode
from ..domain.models import CompactResult, SessionSummary, Turn
from ..infra.config import Config
from .extractor import Extractor


def estimate_tokens(turns: list[Turn], summary: SessionSummary | None = None) -> int:
    """粗略估算。中文按 1 字 ≈ 0.7 token，再给每轮固定开销。

    不必精确：这里只用来决定"要不要压"，差 20% 不影响行为。
    """
    chars = sum(len(t.text) for t in turns)
    if summary:
        chars += len(summary.text)
    return int(chars * 0.7) + 12 * len(turns)


class Compactor:
    def __init__(self, cfg: Config, extractor: Extractor) -> None:
        self._cfg = cfg
        self._extractor = extractor

    def should(self, turns: list[Turn], summary: SessionSummary | None) -> bool:
        if len(turns) <= self._cfg.k_tail_turns:
            return False  # 还没到能压的长度，硬压会把整段对话压没

        # 条数先到就先压。
        # 条数和 token 谁先到算谁：只看 token 时，中间那段既不在历史也不在摘要里。
        if len(turns) > self._cfg.compact_window_turns:
            return True

        return estimate_tokens(turns, summary) > self._cfg.t_compact

    def compact(
        self, turns: list[Turn], summary: SessionSummary | None, *, npc_id: str, session_id: str
    ) -> CompactResult | None:
        total = len(turns)
        if total <= self._cfg.k_tail_turns:
            return None

        # 一次只压前文的 compact_ratio（默认 40%），剩下的继续留原文。
        # 一次压完等于把整段对话换成一句摘要，细节全丢；
        # 渐进式压缩虽然要多调几次，但每次损失都留在可控范围内。
        compress_n = min(
            self._cfg.compact_max_turns,
            max(1, int(total * self._cfg.compact_ratio)),
        )
        keep = max(self._cfg.k_tail_turns, total - compress_n)

        head = turns[: total - keep]
        if not head:
            return None

        result = self._extractor.extract(
            head, npc_id=npc_id, session_id=session_id,
            last_summary=summary, mode=ExtractMode.COMPACT,
        )

        level = (summary.level + 1) if summary else 0
        dropped = level > self._cfg.max_summary_level

        text = result.summary or _skeleton(head)
        if dropped:
            text = f"（更早的对话已略）{text}"[: self._cfg.summary_max_chars]

        new_summary = SessionSummary(
            level=min(level, self._cfg.max_summary_level + 1),
            text=text,
            covered=(head[0].idx, head[-1].idx),
        )
        return CompactResult(
            summary=new_summary,
            facts=result.facts,
            consumed_upto=head[-1].idx,
            dropped_old_summary=dropped,
        )


def _skeleton(turns: list[Turn]) -> str:
    """LLM 没给摘要时的兜底：至少保住"聊过什么话题"。"""
    preview = " / ".join(t.text[:20] for t in turns[-4:])
    return f"（摘要缺失）此前聊到：{preview}"
