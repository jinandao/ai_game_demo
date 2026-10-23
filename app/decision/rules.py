"""决策域：规则剪枝，模型只看到当前合法的意图。"""

from __future__ import annotations

from ..domain.enums import DecisionIntent, Stage
from ..domain.models import NpcState, Profile
from ..relation.affection import AffectionEngine

# 每个意图所需的最低阶段
NEEDS_STAGE: dict[DecisionIntent, Stage] = {
    DecisionIntent.INVITE_WALK: Stage.ACQUAINTANCE,
    DecisionIntent.INVITE_VISIT: Stage.CLOSE,
    DecisionIntent.REQUEST_HUG: Stage.AMBIGUOUS,
}

_ORDER = [Stage.STRANGER, Stage.ACQUAINTANCE, Stage.CLOSE, Stage.AMBIGUOUS, Stage.DEPENDENT]

# 这些是"日常状态"，不是"事件"。它们本来就该反复出现，
# 参与冷却等于把 NPC 的选择剪到只剩两个来回切——看起来就是"一直走来走去"。
COOLDOWN_EXEMPT = {
    DecisionIntent.IDLE,
    DecisionIntent.WORK,
    DecisionIntent.SOLO,
    DecisionIntent.REST,
    DecisionIntent.WITHDRAW,
}


class RuleEngine:
    def __init__(self, affection: AffectionEngine, cooldown_minutes: int = 5) -> None:
        self._affection = affection
        self._cooldown = cooldown_minutes

    def allowed(
        self, *, profile: Profile, npc_id: str, state: NpcState, now
    ) -> set[DecisionIntent]:
        if state.stamina < 0.2:
            return {DecisionIntent.WITHDRAW, DecisionIntent.IDLE}

        stage = self._affection.state_of(profile, npc_id).stage
        allowed = set(DecisionIntent) - set(NEEDS_STAGE)

        for intent, need in NEEDS_STAGE.items():
            if _ORDER.index(stage) >= _ORDER.index(need):
                allowed.add(intent)

        # 冷却只约束"会打扰玩家的一次性动作"（找人、提约定、邀请）。
        # 日常状态不进冷却，否则每 20 秒决策一次、却 30 分钟不许重复，
        # 可用意图会被剪到只剩两三个，行为立刻变得单调。
        last = state.last_decision
        if last and last.intent not in COOLDOWN_EXEMPT and state.last_decision_at:
            gap = (now - state.last_decision_at).total_seconds() / 60
            if gap < self._cooldown:
                allowed.discard(last.intent)

        return allowed
