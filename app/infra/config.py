"""基础设施：配置。所有魔法数字集中在这里，禁止散落在业务代码里。"""

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
    # 触发线按绝对值算，不按上下文窗口比例：系统提示词本身已占几千 token，
    # 再堆对话会稀释前面的约束。
    # 可用 --compact-tokens / --compact-ratio 覆盖。
    compact_trigger_tokens: int = 6000
    compact_ratio: float = 0.4         # 一次压掉前文（不含尾轮）的 40%
    compact_max_turns: int = 24        # 单次最多压多少轮，防止一次喂给模型太多
    k_tail_turns: int = 6              # 最近 N 轮原文不参与压缩
    # 历史窗口：对话消息超过这个数就先压，不等 token 攒够。
    # 因为每轮只把窗口内的内容发给模型，窗口外的既不在历史里、
    # 也还没进摘要——那一段等于凭空消失。
    # 24 条 ≈ 12 轮，配合 num_ctx=8192 后仍有余量（实测约 4300/8192 token）。
    compact_window_turns: int = 24
    max_summary_level: int = 2         # 递归上限，超过丢弃最旧摘要
    summary_max_chars: int = 300

    # ── 向量检索 ──
    # 下面几个都能用命令行/环境变量覆盖，不用改这个文件：
    #   --embedding-model / --embedding-host / --sim-floor / --no-embedding
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_host: str = "http://localhost:11434"
    embedding_timeout: float = 8.0
    embedding_batch: int = 16
    # qwen3-embedding:0.6b 实测：相关 ≈0.57~0.74，无关 ≈0.32~0.38。
    # 换模型必须重新标定，这个值不能照抄。
    sim_floor: float = 0.45

    # ── 记忆检索 ──
    memory_top_k: int = 3
    recency_half_life_days: float = 14.0

    # ── 知识检索 ──
    kb_top_k: int = 3

    # ── 好感度 ──
    max_daily_affection: int = 8
    decay_per_day: float = 0.0        # demo 阶段建议 0，避免演示时手感变差

    @property
    def t_compact(self) -> int:
        """保留这个名字：老代码和调试面板都在用它。"""
        return self.compact_trigger_tokens

    # ── 派生路径 ──
    # 只落"她记住的东西"（memory / profile / npc_state）。
    # 原始对话不写盘：记忆是产品，聊天记录不是。
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
