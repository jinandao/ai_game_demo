"""领域内核：纯数据类。只有字段和序列化，不放业务逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from .enums import ActionId, DecisionIntent, Intent, Scope, Stage, Subject


# ─────────────────────────── 会话 ───────────────────────────


@dataclass(frozen=True)
class Turn:
    """一轮原始对话。idx 是会话内序号，压缩区间与幂等都靠它。"""

    idx: int
    role: Literal["player", "npc"]
    text: str
    ts: datetime
    meta: dict[str, Any] = field(default_factory=dict)  # intent / action_id / tool_trace


@dataclass(frozen=True)
class SessionSummary:
    """L1 压缩摘要。只服务本次会话的连贯性，永不写入 L2。"""

    level: int                     # 0~2
    text: str
    covered: tuple[int, int]       # 已覆盖的 turn 区间 (start_idx, end_idx)


@dataclass(frozen=True)
class SessionContext:
    """传给 DialoguePipeline 的只读视图。不给 ChatSession 本体，防止越权改状态。"""

    session_id: str
    npc_id: str
    summary: SessionSummary | None
    recent_turns: tuple[Turn, ...]
    turn_count: int
    ambience: str = ""          # 当前场景的客观环境（天气、光线、气味）
    outcome: str = ""           # 她刚为玩家做完的事，写成一句话


# ─────────────────────────── 记忆 ───────────────────────────


@dataclass(frozen=True)
class MemoryItem:
    id: str
    session_id: str
    npc_id: str
    turn_range: tuple[int, int]
    ts: datetime
    subject: Subject
    key: str                  # 去重键，如 "睡眠"
    value: str
    importance: int           # 1~5
    evidence: str             # 原句片段，防瞎编
    source: str = "dialogue"  # dialogue | action | decision | fallback
    promise: bool = False     # 是否为约定 / 未完成事项
    dedupe: bool = True       # 是否参与 key 去重（行为流水不参与）
    supersedes: str | None = None


@dataclass(frozen=True)
class Hit:
    """L2 / L3 共用的检索命中。"""

    text: str
    score: float
    source: str                       # "memory:m_0007" / "kb:world#3"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FactItem:
    """compose_facts 的产出单元：中性陈述 + 可追溯来源。"""

    statement: str
    provenance: str


@dataclass(frozen=True)
class Facts:
    """中性事实。kind 告诉 Stylist 如何使用：normal / empty / out_of_scope / action_proposal / action_denied。"""

    items: tuple[FactItem, ...]
    confidence: float
    is_empty: bool = False
    kind: str = "normal"

    @classmethod
    def empty(cls, confidence: float = 0.0) -> Facts:
        return cls(items=(), confidence=confidence, is_empty=True, kind="empty")

    @classmethod
    def of(cls, statements: list[str], *, kind: str = "normal", provenance: str = "policy") -> Facts:
        return cls(
            items=tuple(FactItem(statement=s, provenance=provenance) for s in statements),
            confidence=1.0,
            kind=kind,
        )


@dataclass(frozen=True)
class AffectionHint:
    """抽取产出的建议值。不能直接改好感度，必须过 AffectionEngine。"""

    delta: int
    reason: str


@dataclass(frozen=True)
class ExtractResult:
    summary: str | None
    facts: tuple[MemoryItem, ...] = ()
    hints: tuple[AffectionHint, ...] = ()
    degraded: bool = False            # JSON 解析失败走了 RAW 兜底


@dataclass(frozen=True)
class CompactResult:
    summary: SessionSummary
    facts: tuple[MemoryItem, ...]
    consumed_upto: int                # 被压到第几轮
    dropped_old_summary: bool         # 超过 MAX_LEVEL 丢弃最旧摘要


# ─────────────────────────── 关系 ───────────────────────────


@dataclass
class AffectionState:
    affinity: int = 0
    stage: Stage = Stage.STRANGER
    daily_gain: int = 0
    last_gain: date | None = None


@dataclass
class Profile:
    """L4 关系状态。只有 AffectionEngine 能改 affection / stage。"""

    player_id: str
    name: str
    affection: dict[str, AffectionState] = field(default_factory=dict)  # npc_id -> state
    flags: dict[str, str] = field(default_factory=dict)                 # 节点完成标记
    last_session_at: datetime | None = None


# ─────────────────────────── 对话 ───────────────────────────


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    scope: Scope | None = None
    action_id: ActionId | None = None
    # 模型自己决定要不要查、查什么。空元组 = 这一轮不检索，一次 embed 都不发。
    need: tuple[str, ...] = ()
    query: str = ""                  # 检索用的词，模型改写过的


# ─────────────────────────── 决策 ───────────────────────────


@dataclass(frozen=True)
class Decision:
    intent: DecisionIntent
    reason: str
    urgency: float
    about_memory: str | None = None   # SHARE_MEMORY 指向哪条记忆 id
    action: str | None = None         # SERVE 指向哪个照料动作（services.py）
    script: dict[str, Any] | None = None
    """行为脚本（NPC 自己输出的、符合约定格式的 JSON）。

    决策层只负责给出"想做什么 + 怎么做"的结构化描述，不生成台词；
    解析成行为树并执行由 BehaviorDirector 负责。
    script 允许缺省——Director 会按 intent 退回 FALLBACK 模板。
    """


@dataclass
class NpcState:
    npc_id: str
    stamina: float = 1.0
    mood: float = 0.5
    last_decision_at: datetime | None = None
    last_decision: Decision | None = None
    cooldowns: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class NpcEvent:
    """决策层产出、交给 UI 的"她想做什么"。台词由对话层演绎。"""

    npc_id: str
    decision: Decision
    opener_facts: Facts | None = None


# ─────────────────────────── 动作 ───────────────────────────


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    needs_consent: bool
    required_stage: Stage


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    action_id: ActionId
    detail: str
    memory: MemoryItem | None = None   # 执行成功后要写入的记忆


@dataclass(frozen=True)
class ActionContext:
    player: Profile
    npc_id: str
    session_id: str
    consent_granted: bool


# ─────────────────────────── 人设 ───────────────────────────


@dataclass(frozen=True)
class PersonaConflict:
    """人设 × 职责的冲突点。这一块比"她很好"值钱得多。"""

    name: str                 # 例："过度承诺"
    trigger: str              # 什么时候会犯
    ruling: str               # 裁决线，必须可判定


@dataclass(frozen=True)
class Persona:
    npc_id: str
    name: str
    identity: str                        # 身份 / 立场，不可违背
    must: tuple[str, ...] = ()           # 职责优先级最高
    must_not: tuple[str, ...] = ()
    tone: str = ""
    small_talk: str = ""                 # ⭐ 闲聊时怎么聊：节奏、话题偏好、回避什么
    charm: tuple[str, ...] = ()          # ⭐ 萌点：自然流露，不许主动说出来
    address: str = ""                    # 怎么称呼玩家
    habits: tuple[str, ...] = ()
    banned: tuple[str, ...] = ()         # 一出现就出戏的套话
    ignorance_excuse: str = ""           # ⭐ 合角色的"不知道"出口
    conflicts: tuple[PersonaConflict, ...] = ()
    few_shots: tuple[tuple[str, str], ...] = ()
    """按关系分组的两套示范。

    few_shots           —— 还不熟的时候怎么说话（客气、简短、有距离）
    few_shots_intimate  —— 关系近了之后才有的口吻（别扭、口是心非）

    傲娇不是基础人格，是关系到位后才解锁的。两套分开，模型才不会
    在第一次见面就用「才没有」。
    """
    few_shots_intimate: tuple[tuple[str, str], ...] = ()

    def has_banned(self, text: str) -> list[str]:
        return [w for w in self.banned if w and w in text]


# ─────────────────────────── 会话收尾 ───────────────────────────



@dataclass(frozen=True)
class EndReport:
    """end() 的返回值。给 UI 做一句总结，也给调试面板核对。"""

    session_id: str
    turns: int
    facts_written: int
    affection_delta: int
    degraded: bool
