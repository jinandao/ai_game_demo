"""配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class Config:
    # ── 路径 ──
    data_dir: Path = Path("data")
    persona_dir: Path = Path("persona/cards")
    kb_dir: Path = Path("kb")

    # ── 压缩 ──
    compact_trigger_tokens: int = 6000
    compact_ratio: float = 0.4         # 一次压掉前文（不含尾轮）的 40%
    compact_max_turns: int = 24        # 单次最多压多少轮，防止一次喂给模型太多
    k_tail_turns: int = 6              # 最近 N 轮原文不参与压缩
    # 历史窗口：对话消息超过这个数就先压，不等 token 攒够。
    compact_window_turns: int = 24
    max_summary_level: int = 2         # 递归上限，超过丢弃最旧摘要
    summary_max_chars: int = 300

    # ── 向量检索 ──
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_host: str = "http://localhost:11434"
    embedding_timeout: float = 8.0
    embedding_batch: int = 16
    # qwen3-embedding:0.6b 实测：相关 ≈0.57~0.74，无关 ≈0.32~0.38。
    sim_floor: float = 0.45

    # ── 记忆检索 ──
    memory_top_k: int = 3
    recency_half_life_days: float = 14.0

    # ── 记忆分档 ──
    event_memory_cap: int = 50
    deep_action_memory_cap: int = 50
    # 填给模型看几条。决策要"最近做过什么 + 深刻的事"；主动开口要"挑一条说"。
    decision_event_k: int = 5
    decision_deep_k: int = 5
    chat_deep_k: int = 5
    chat_interaction_k: int = 5

    # ── 知识检索 ──
    kb_top_k: int = 3

    # ── 好感度 ──
    max_daily_affection: int = 8
    decay_per_day: float = 0.0        # demo 阶段建议 0，避免演示时手感变差

    # ── 决策：意图从哪来 ──
    intent_mode: str = "rule"

    @property
    def t_compact(self) -> int:
        """保留这个名字：老代码和调试面板都在用它。"""
        return self.compact_trigger_tokens

    # ── 派生路径 ──
    def player_memory_path(self, player_id: str) -> Path:
        # 一个 JSON 数组，进游戏加载、退游戏保存
        return self.data_dir / "memories" / f"{player_id}.json"

    def memory_index_path(self, player_id: str) -> Path:
        return self.data_dir / "memories" / f"{player_id}.index.json"

    def profile_path(self, player_id: str) -> Path:
        return self.data_dir / "profile" / f"{player_id}.json"

    def npc_state_path(self, npc_id: str) -> Path:
        return self.data_dir / "npc_state" / f"{npc_id}.json"

    def ensure_dirs(self) -> None:
        for sub in ("memories", "profile", "npc_state"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
