"""入口：Pygame 版。

    python main.py                 # 连本地 ollama
    python main.py --mock          # 不连模型，只验证界面与流程
"""

from __future__ import annotations

import argparse
import os
import sys

# 必须在 pygame / SDL 初始化之前设置。
# SDL2 默认不显示输入法的候选框，中文只能盲打；这个 hint 让系统 IME 的
# 候选词窗口跟到游戏窗口里来。命令行单独设置也生效。
os.environ.setdefault("SDL_IME_SHOW_UI", "1")


def _env_float(name: str, fallback: float) -> float:
    try:
        return float(os.getenv(name, fallback))
    except ValueError:
        return fallback


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, fallback))
    except ValueError:
        return fallback


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="残响都市 · AI NPC demo")
    p.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "ollama"),
                   choices=["ollama", "openai", "mock"])
    p.add_argument("--model", default=os.getenv("LLM_MODEL", "qwen3.8:27b-mtp-q4_K_M"))
    p.add_argument("--host", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    p.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    p.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    p.add_argument("--player", default="P1001")
    p.add_argument("--mock", action="store_true", help="等价于 --provider mock")
    p.add_argument(
        "--think", action=argparse.BooleanOptionalAction, default=None,
        help="推理模型是否先思考。默认对 qwen3 等自动关闭（会吃掉输出预算）",
    )

    # ── 检索 ──
    p.add_argument(
        "--embedding-model",
        default=os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b"),
        help="嵌入模型名（ollama pull 过的）",
    )
    p.add_argument(
        "--embedding-host",
        default=os.getenv("EMBEDDING_HOST", ""),
        help="嵌入服务地址，默认跟 --host 一致",
    )
    p.add_argument(
        "--no-embedding", action="store_true",
        help="关闭向量检索，退回关键词匹配",
    )
    p.add_argument(
        "--sim-floor", type=float, default=_env_float("SIM_FLOOR", 0.45),
        help="相似度下限，低于它当作检索不到",
    )

    # ── 压缩 ──
    p.add_argument(
        "--compact-tokens", type=int, default=_env_int("COMPACT_TOKENS", 6000),
        help="会话估算 token 超过这个值就压缩",
    )
    p.add_argument(
        "--compact-ratio", type=float, default=_env_float("COMPACT_RATIO", 0.4),
        help="一次压掉前文的百分比（0~1）",
    )
    return p.parse_args()


THINKING_MODELS = ("qwen3", "deepseek-r1", "reasoning")


def _resolve_think(args: argparse.Namespace) -> bool | None:
    """决定是否给 ollama 传 think。推理模型默认关闭，避免思考段吃掉 num_predict。"""
    if args.think is not None:
        return args.think
    name = args.model.lower()
    return False if any(k in name for k in THINKING_MODELS) else None


def build_client(args: argparse.Namespace):
    from app.infra.llm import MockLLM, OllamaClient, OpenAIClient

    if args.mock or args.provider == "mock":
        print("[提示] mock 模式：模型不会真的回复，界面只用来验证流程。")
        return MockLLM(
            {},
            json_default={
                "intent": "chitchat",
                "reason": "mock",
                "urgency": 0.2,
                "behavior": {"op": "idle"},
                "summary": "（mock 摘要）",
                "facts": [],
            },
        )

    if args.provider == "openai":
        return OpenAIClient(
            model=args.model, api_key=args.api_key, base_url=args.base_url
        )

    return OllamaClient(model=args.model, host=args.host, think=_resolve_think(args))


def main() -> int:
    args = parse_args()

    try:
        import pygame  # noqa: F401
    except ImportError:
        print("缺少 pygame。先装一下：\n\n    pip install pygame\n", file=sys.stderr)
        return 1

    from app.factory import build
    from app.infra.config import Config
    from app.ui.app_window import AppWindow

    client = build_client(args)

    if not client.ping():
        print(
            f"[警告] 连不上模型（{args.model}）。\n"
            f"  本地 ollama：确认 `ollama serve` 在跑，并且 `ollama pull {args.model}` 已完成。\n"
            f"  也可以先用 `python main.py --mock` 看看界面。\n",
            file=sys.stderr,
        )

    cfg = Config(
        embedding_model=args.embedding_model,
        sim_floor=args.sim_floor,
        compact_trigger_tokens=args.compact_tokens,
        compact_ratio=args.compact_ratio,
    )
    # 嵌入服务默认跟推理服务同机；换 --host 时不用再单独指一次
    if args.embedding_host:
        cfg.embedding_host = args.embedding_host
    elif args.provider == "ollama":
        cfg.embedding_host = args.host

    use_embedding = not (args.mock or args.provider == "mock" or args.no_embedding)

    app = build(
        cfg,
        llm=client,
        player_id=args.player,
        use_embedding=use_embedding,
    )

    print(f"[启动] 模型 {args.model}，人设 {list(app.deps.personas)}，场景 {len(app.deps.world.all())} 个")
    if args.provider == "ollama":
        think = _resolve_think(args)
        label = {True: "开", False: "关（推理模型建议关）", None: "不传"}[think]
        print(f"[配置] 思考模式 {label}")
    if use_embedding:
        print(
            f"[配置] 检索 向量 · {cfg.embedding_model} · 阈值 {cfg.sim_floor}"
            f" · {cfg.embedding_host}"
        )
    else:
        print("[配置] 检索 关键词（向量已关闭）")
    print(
        f"[配置] 压缩 {cfg.compact_trigger_tokens} token 触发"
        f" · 一次压 {int(cfg.compact_ratio * 100)}%"
        f" · 尾轮保留 {cfg.k_tail_turns}"
    )
    print("[提示] 点地面移动，点人物对话，点建筑进门；F1 调试，F2 推进一小时。")

    AppWindow(app).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
