"""组装根。全项目唯一创建具体实现的地方。

换模型、存储或检索方式，只改这里。
"""

from __future__ import annotations

from pathlib import Path

from .action.gate import ActionGate
from .action.tools import default_tools
from .behavior import World, base_world
from .decision.planner import DecisionPlanner
from .decision.rules import RuleEngine
from .decision.state import NpcStateStore
from .dialogue.composer import Composer
from .dialogue.handlers import default_handlers
from .dialogue.pipeline import DialoguePipeline
from .dialogue.router import IntentRouter
from .dialogue.stylist import Stylist
from .domain.enums import Scope
from .game import Deps, GameApp
from .infra.clock import Clock, FrozenClock, SystemClock
from .infra.config import Config
from .infra.embedding import OllamaEmbedder
from .infra.llm import LLMClient, MockLLM, OllamaClient
from .knowledge.base import Chunk, KnowledgeBase, VectorKB
from .knowledge.keyword import KeywordKB
from .knowledge.loading import load_kb
from .memory.compactor import Compactor
from .memory.extractor import Extractor
from .memory.retriever import MemoryRetriever
from .memory.store import JsonMemoryStore, ProfileStore
from .persona.builtin import PERSONAS
from .relation.affection import AffectionEngine


def build(
    cfg: Config | None = None,
    *,
    llm: LLMClient | None = None,
    clock: Clock | None = None,
    kb: KnowledgeBase | None = None,
    world: World | None = None,
    player_id: str = "P1001",
    model: str = "qwen2.5:7b",
    use_embedding: bool = True,
) -> GameApp:
    cfg = cfg or Config()
    cfg.ensure_dirs()
    clock = clock or SystemClock()
    llm = llm or OllamaClient(model=model)

    # 嵌入模型只在真模型模式下有意义；mock 模式跳过，免得启动时白等超时
    embedder = (
        OllamaEmbedder(
            model=cfg.embedding_model,
            host=cfg.embedding_host,
            timeout=cfg.embedding_timeout,
            batch=cfg.embedding_batch,
        )
        if use_embedding
        else None
    )

    store = JsonMemoryStore(cfg, player_id)
    profiles = ProfileStore(cfg)
    affection = AffectionEngine(cfg, clock)
    retriever = MemoryRetriever(store, cfg, clock, embedder)
    extractor = Extractor(llm, cfg, clock)
    compactor = Compactor(cfg, extractor)
    composer = Composer()
    stylist = Stylist(llm)

    tools = default_tools()
    gate = ActionGate(tools, affection)

    if kb is None:
        chunks = load_kb(cfg.kb_dir)
        vector_kb = VectorKB(
            chunks, embedder, sim_floor=cfg.sim_floor, fallback=KeywordKB(chunks)
        )
        if vector_kb.warmup():
            kb = vector_kb
            stats = vector_kb.stats()
            print(f"[检索] 向量索引就绪：{stats['chunks']} 块 / {stats['dim']} 维")
        else:
            stats = vector_kb.stats()
            print(f"[检索] 向量索引未建立（{stats['reason']}），退回关键词检索")
            kb = KeywordKB(chunks)

    pipeline = DialoguePipeline(
        router=IntentRouter(llm),
        handlers=default_handlers(
            kb=kb,
            retriever=retriever,
            gate=gate,
            tools=tools,
            composer=composer,
            on_action=lambda item: store.append([item]),
        ),
        stylist=stylist,
        retriever=retriever,
        gate=gate,
    )

    npc_states = NpcStateStore(cfg)
    planner = DecisionPlanner(llm, RuleEngine(affection), retriever, clock, PERSONAS)

    deps = Deps(
        cfg=cfg,
        clock=clock,
        llm=llm,
        personas=PERSONAS,
        store=store,
        profiles=profiles,
        retriever=retriever,
        pipeline=pipeline,
        stylist=stylist,
        compactor=compactor,
        extractor=extractor,
        affection=affection,
        gate=gate,
        tools=tools,
        planner=planner,
        npc_states=npc_states,
        world=world or base_world(),
    )
    return GameApp(deps, player_id=player_id)


def build_for_tests(cfg: Config | None = None, **kwargs) -> GameApp:
    """零模型依赖的版本。所有流程测试都从这个入口进。"""
    cfg = cfg or Config(data_dir=Path("data/test"))
    return build(
        cfg,
        llm=MockLLM({}),
        clock=FrozenClock(),
        kb=KeywordKB(
            [
                Chunk(
                    id="world#1",
                    text="云塌之后城市安静了下来，站外的地方要戴耳塞才能去。",
                    scope=Scope.WORLD,
                )
            ]
        ),
        use_embedding=False,      # 测试不连 ollama，进程要秒开
        **kwargs,
    )
