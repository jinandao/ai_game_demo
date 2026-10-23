"""对话域：内容层，把检索命中写成中性要点。"""

from __future__ import annotations

from ..domain.enums import Intent
from ..domain.models import FactItem, Facts, Hit


class Composer:
    def __init__(self, max_items: int = 3) -> None:
        self._max_items = max_items

    def compose(self, hits: list[Hit], *, intent: Intent) -> Facts:
        items: list[FactItem] = []
        for h in hits[: self._max_items]:
            statement = _normalize(h)
            if statement:
                items.append(FactItem(statement=statement, provenance=h.source))

        if not items:
            return Facts.empty(confidence=hits[0].score if hits else 0.0)

        confidence = sum(h.score for h in hits[: len(items)]) / len(items)
        return Facts(items=tuple(items), confidence=min(1.0, confidence))

    def compose_empty(self, reason: str) -> Facts:
        return Facts.empty(confidence=0.0)


def _normalize(hit: Hit) -> str:
    """去掉来源前缀、压掉换行。命中文本本身已经是中性陈述。"""
    text = hit.text.strip().replace("\n", " ")
    return text[:200]
