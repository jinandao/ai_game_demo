"""决策域：NPC 状态持久化。决策结果落盘，供对话层开场时读取。"""

from __future__ import annotations

from datetime import datetime

from ..domain.enums import DecisionIntent
from ..domain.models import Decision, NpcState
from ..infra.config import Config
from ..infra.storage import JsonFile


class NpcStateStore:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def load(self, npc_id: str) -> NpcState:
        raw = JsonFile(self._cfg.npc_state_path(npc_id)).load()
        if not raw:
            return NpcState(npc_id=npc_id)
        last = raw.get("last_decision")
        return NpcState(
            npc_id=npc_id,
            stamina=float(raw.get("stamina", 1.0)),
            mood=float(raw.get("mood", 0.5)),
            last_decision_at=_ts(raw.get("last_decision_at")),
            last_decision=Decision(
                intent=DecisionIntent(last["intent"]),
                reason=last.get("reason", ""),
                urgency=float(last.get("urgency", 0.5)),
                about_memory=last.get("about_memory"),
            )
            if last
            else None,
            cooldowns={k: _ts(v) for k, v in raw.get("cooldowns", {}).items() if _ts(v)},
        )

    def save(self, state: NpcState) -> None:
        d = state.last_decision
        JsonFile(self._cfg.npc_state_path(state.npc_id)).save(
            {
                "npc_id": state.npc_id,
                "stamina": state.stamina,
                "mood": state.mood,
                "last_decision_at": state.last_decision_at.isoformat()
                if state.last_decision_at
                else None,
                "last_decision": {
                    "intent": d.intent.value,
                    "reason": d.reason,
                    "urgency": d.urgency,
                    "about_memory": d.about_memory,
                }
                if d
                else None,
                "cooldowns": {k: v.isoformat() for k, v in state.cooldowns.items()},
            }
        )


def _ts(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None
