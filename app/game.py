"""应用层：GameApp，UI 的唯一入口。

UI 只调用这里的方法，不直接接触 memory / infra。
"""

from __future__ import annotations

import queue
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from .action import services
from .action.gate import ActionGate
from .action.tools import ActionTool
from .behavior import (
    BehaviorDirector,
    BehaviorScriptParser,
    BehaviorSystem,
    SpeakRequest,
    World,
    WorldEvent,
    base_world,
    default_registry,
)
from .decision.planner import DecisionPlanner
from .decision.queue import DecisionQueue
from .decision.state import NpcStateStore
from .dialogue.pipeline import DialoguePipeline
from .dialogue.stylist import Stylist
from .domain.enums import ActionId, DecisionIntent, Subject
from .domain.models import (
    Decision,
    EndReport,
    Facts,
    MemoryItem,
    NpcEvent,
    Persona,
    Profile,
    Turn,
)
from .infra.clock import Clock
from .infra.config import Config
from .infra.llm import LLMClient
from .memory.compactor import Compactor
from .memory.extractor import Extractor
from .memory.retriever import MemoryRetriever
from .memory.session import ChatSession
from .memory.store import MemoryStore, ProfileStore, new_memory_id
from .relation.affection import AffectionEngine


@dataclass
class Deps:
    """组装根产出的依赖集合。GameApp 只读它，不自己 new 具体类。"""

    cfg: Config
    clock: Clock
    llm: LLMClient
    personas: dict[str, Persona]
    store: MemoryStore
    profiles: ProfileStore
    retriever: MemoryRetriever
    pipeline: DialoguePipeline
    stylist: Stylist
    compactor: Compactor
    extractor: Extractor
    affection: AffectionEngine
    gate: ActionGate
    tools: dict[ActionId, ActionTool]
    planner: DecisionPlanner
    npc_states: NpcStateStore
    world: World = None  # type: ignore[assignment]
    decisions: DecisionQueue = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.world is None:
            self.world = base_world()
        if self.decisions is None:
            self.decisions = DecisionQueue(self.planner)


class GameApp:
    def __init__(self, deps: Deps, player_id: str = "P1001") -> None:
        self.deps = deps
        self.profile: Profile = deps.profiles.load(player_id)
        self.session: ChatSession | None = None
        # UI 可以把说话改成异步：返回占位文本，真正的台词稍后回调
        self.speech_hook = None

        # 进游戏：把记忆数组从文件读进内存。之后运行期只改内存
        deps.store.load_from_disk()

        # 决策队列把结果丢回这个队列，由 UI 线程在 drain_decisions() 里生效
        self._decision_results: queue.Queue[list[NpcEvent]] = queue.Queue()
        deps.decisions.set_sink(self._on_decisions)

        self.events: list[WorldEvent] = []
        # 她刚做完的事：下次开口时写进对话的「刚刚发生」，
        # 否则她做完回来就忘了自己干过什么。
        self.pending_outcome: dict[str, str] = {}
        self.behavior = BehaviorSystem(
            world=deps.world,
            director=BehaviorDirector(BehaviorScriptParser(default_registry())),
            clock=deps.clock,
            speech=self._speak,
            emit=self._emit,
        )
        # 初始位置与速度按人设给
        initial = {
            "A": ("outdoor", (820.0, 600.0), 180.0),
            "B": ("workshop", (420.0, 520.0), 120.0),
        }
        for npc_id in deps.personas:
            scene, xy, speed = initial.get(npc_id, ("outdoor", (720.0, 600.0), 150.0))
            self.behavior.spawn(npc_id, scene=scene, xy=xy, speed=speed)

        # 一进游戏就让人动起来。否则要等到第一次决策（20 秒后）才有动作，
        # 玩家看到的就是两个静止的色块，会以为游戏卡死了。
        self._prime_behaviors()

    def _prime_behaviors(self) -> None:
        for npc_id in self.deps.personas:
            if self.behavior.runner(npc_id).root is None:
                self.behavior.apply(
                    npc_id,
                    Decision(DecisionIntent.WORK, "startup", 0.5),
                )

    def _emit(self, event: WorldEvent) -> None:
        self.events.append(event)
        if event.kind == "delivery":
            outcome = str(event.payload.get("outcome") or "").strip()
            if outcome:
                self.pending_outcome[event.npc_id] = outcome

    def accept_service(self, npc_id: str, action_id: str) -> bool:
        """玩家确认后开始执行。"""
        return self.perform(npc_id, action_id)

    def perform(self, npc_id: str, action_id: str) -> bool:
        """装载照料脚本：走开 → 停一下 → 回来 → 交付。"""
        svc = services.get(action_id)
        if svc is None or svc.npc_id != npc_id:
            return False
        self.behavior.apply(
            npc_id,
            Decision(
                intent=DecisionIntent.SOLO,
                reason=f"serve:{action_id}",
                urgency=1.0,
                action=action_id,
                script=services.perform_script(svc),
            ),
        )
        return True

    # ── 会话 ──

    def warmup(self) -> str:
        """发一条最小请求：验证链路并把模型载入显存。"""
        t0 = time.monotonic()
        self.deps.llm.complete(
            [{"role": "user", "content": "回一个字：好"}],
            temperature=0.0,
            max_tokens=4,
        )
        return f"{time.monotonic() - t0:.1f}s"

    def _on_decisions(self, events: dict[str, NpcEvent]) -> None:
        """在决策 worker 线程里被调用。只做搬运，不动游戏状态。"""
        self._decision_results.put(list(events.values()))

    # ── 会话 ──

    def enter_chat(self, npc_id: str, *, opening: str | None = None) -> ChatSession:
        """opening：她主动开口的那一句。会作为第 0 轮写进 transcript，
        这样玩家点进来之后，她的回答能接上自己刚说的话。"""
        if self.session and not self.session.is_closed:
            self._snapshot_convo()
            self.session.end()  # 上一段没关就再进，按结束处理，不留悬挂状态

        persona = self.deps.personas[npc_id]
        # 按她所在场景注入环境描述
        scene = self.deps.world.scene(self.behavior.agent(npc_id).place)
        self.session = ChatSession(
            session_id=f"s_{uuid.uuid4().hex[:8]}",
            player=self.profile,
            persona=persona,
            pipeline=self.deps.pipeline,
            compactor=self.deps.compactor,
            extractor=self.deps.extractor,
            store=self.deps.store,
            affection=self.deps.affection,
            cfg=self.deps.cfg,
            clock=self.deps.clock,
            ambience=scene.ambience,
            # 她刚给他做过的事。没有就空着，不要凭空写「刚刚发生」
            outcome=self.pending_outcome.pop(npc_id, ""),
        )
        if opening:
            self.session.preload_npc_turn(opening)

        # 聊天优先：本地模型一次只能跑一个请求，决策继续排队就等它说完。
        # 放在最后一步，前面任何一步失败都不会把队列永久停住。
        self.deps.decisions.pause()
        return self.session

    def send(self, text: str, on_delta=None) -> Turn:
        if not self.session or self.session.is_closed:
            raise RuntimeError("没有打开的会话")
        return self.session.send(text, on_delta=on_delta)

    def agree(self, action_id: ActionId) -> None:
        if self.session and not self.session.is_closed:
            self.session.grant_consent(action_id)

    def close_chat(self) -> EndReport | None:
        session = self.session
        if session is None or session.is_closed:
            self.deps.decisions.resume()
            return None

        # 先把会话摘下来：收尾要跑一次完整的抽取（几十秒），
        # 这期间 UI 已经关了对话框，玩家很可能马上再点一次 NPC。
        # 那时 self.session 已被摘掉，会新建一个，不会撞上正在收尾的这个。
        self.session = None
        self.deps.decisions.resume()        # 先恢复自主决策，别让 NPC 陪等

        try:
            report = session.end()
        finally:
            self.deps.profiles.save(self.profile)
            self.deps.store.save_to_disk()  # 会话结束即落盘，防崩溃丢失
        return report

    def shutdown(self) -> None:
        """退出游戏：把还没结束的会话收尾，再把记忆数组写回文件。"""
        if self.session and not self.session.is_closed:
            try:
                self.session.end()
            except Exception:  # noqa: BLE001  退出路径不该因为抽取失败而卡住
                pass
        self.deps.decisions.shutdown()
        self.deps.store.save_to_disk()
        self.deps.profiles.save(self.profile)

    # ── 时间与自主行为 ──

    def idle_tick(self) -> NpcEvent | None:
        """把待决策的 NPC 交给队列后立即返回；推理在 worker 线程完成。"""
        # 对话中不攒决策：pause 只是不发，攒着会在对话结束后一次性涌出来
        if self.deps.decisions.paused:
            return None

        for npc_id in self.deps.personas:
            runner = self.behavior.runner(npc_id)
            if runner.is_occupied:
                continue

            state = self.deps.npc_states.load(npc_id)
            # 给模型看场景的中文名，不是 outdoor / workshop 这种 id
            here = self.deps.world.scene(self.behavior.agent(npc_id).place).name
            player_at = self.deps.world.scene(self.behavior.player_place).name
            self.deps.decisions.submit(npc_id, self.profile, state, here, player_at)
        return None

    def walk_to(self, npc_id: str, scene_id: str) -> None:
        """让她带路去某场景。用脚本直接下指令，不再经过模型。"""
        script = {
            "op": "seq",
            "children": [
                {"op": "move", "place": scene_id},
                {"op": "wait", "seconds": 5},
            ],
        }
        self.behavior.apply(
            npc_id,
            Decision(
                intent=DecisionIntent.SOLO,
                reason="goto",
                urgency=1.0,
                script=script,
            ),
        )

    def drain_decisions(self) -> list[NpcEvent]:
        """UI 线程每帧调。所有游戏状态变更都在这里，保证只有 UI 线程写状态。"""
        out: list[NpcEvent] = []
        while True:
            try:
                out.extend(self._decision_results.get_nowait())
            except queue.Empty:
                break

        for event in out:
            state = self.deps.npc_states.load(event.npc_id)
            state.last_decision = event.decision
            state.last_decision_at = self.deps.clock.now()
            self.deps.npc_states.save(state)

            self._record_decision(event.npc_id, event.decision)
            self.behavior.apply(event.npc_id, event.decision)
        return out

    def flush_decisions(self, timeout: float = 60.0) -> list[NpcEvent]:
        """同步等队列跑完。给 CLI / 测试用，GUI 不走这条路。"""
        collected: list[NpcEvent] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.deps.decisions.tick()
            collected.extend(self.drain_decisions())
            stats = self.deps.decisions.stats()
            if not stats["busy"] and int(stats["pending"]) == 0:
                return collected
            time.sleep(0.1)
        return collected

    # ── 行为推进 ──

    def _record_decision(self, npc_id: str, decision) -> None:
        """记一条事件记忆，供下一轮决策和重启后读取；dedupe=False。"""
        now = self.deps.clock.now()
        stamp = int(now.timestamp() * 1000)
        self.deps.store.append(
            [
                MemoryItem(
                    id=new_memory_id(),
                    session_id=f"decision:{npc_id}",
                    npc_id=npc_id,
                    turn_range=(stamp, stamp),
                    ts=now,
                    subject=Subject.EVENT,
                    key=f"决策_{decision.intent.value}",
                    value=decision.intent.value,
                    importance=1,
                    evidence="",
                    source="decision",
                    dedupe=False,
                )
            ]
        )

    def tick(self, dt: float) -> dict[str, object]:
        """游戏循环调用。dt 单位是秒。"""
        return {k: v.value for k, v in self.behavior.update(dt).items()}

    def move_player(self, scene: str, xy: tuple[float, float] | None = None) -> None:
        self.behavior.move_player(scene, xy)

    def drain_events(self) -> list[WorldEvent]:
        """UI 每帧取一次事件（说了什么、走到了哪）。取完即清。"""
        out, self.events = self.events, []
        return out

    def accept_invite(self, npc_id: str, action_id: ActionId) -> None:
        """玩家点了"好啊"。行为层解除等待，会话层记下同意。"""
        self.behavior.resolve_invite(npc_id, True)
        if self.session and not self.session.is_closed and self.session.npc_id == npc_id:
            self.session.grant_consent(action_id)

    def decline_invite(self, npc_id: str) -> None:
        self.behavior.resolve_invite(npc_id, False)

    # ── 行为层说话的能力注入 ──

    def _speak(self, req: SpeakRequest) -> str:
        """行为层说话：行为只给 topic，台词由语气层生成。"""
        if self.speech_hook is not None:
            return self.speech_hook(req)   # UI 走异步，避免卡主循环
        return self.speak_sync(req)

    def speak_sync(self, req: SpeakRequest) -> str:
        persona = self.deps.personas.get(req.npc_id)
        if persona is None:
            return ""
        facts = self._facts_for(req)
        state = self.deps.affection.state_of(self.profile, req.npc_id)
        turns, summary = self._convo_context(req.npc_id)
        return self.deps.stylist.render(
            facts,
            persona=persona,
            recent_turns=turns,
            # 主动开口也要按当前关系说话：陌生人不会在"我给你垫一下"里带傲娇
            stage=state.stage,
            summary=summary,
        )

    def _facts_for(self, req: SpeakRequest) -> Facts | None:
        if req.topic == "share_memory":
            mem = self._find_memory(req.npc_id, req.about)
            if mem is None:
                return Facts.empty()
            return Facts.of([f"你想提起这件事：{mem.value}"])

        if req.topic == "invite":
            label = {
                ActionId.INVITE_WALK.value: "一起在站里走一圈",
                ActionId.INVITE_VISIT.value: "去你那儿坐坐",
                ActionId.REQUEST_HUG.value: "抱一下",
            }.get(req.action or "", "一起待会儿")
            return Facts.of(
                [f"你想邀请玩家{label}，但还没得到同意。先问一句，别假设他答应了"],
                kind="action_proposal",
            )

        if req.topic == "offer":
            svc = services.get(req.action or "")
            label = svc.label if svc else "为他做点什么"
            return Facts.of(
                [
                    f"你想{label}，但还没问他愿不愿意。",
                    "先问一句，问完就停住等他回答。不要自己接着说已经做好了。",
                ],
                kind="action_proposal",
            )

        if req.topic == "delivery":
            svc = services.get(req.action or "")
            outcome = svc.outcome if svc else "你刚为他做完了一件事。"
            return Facts.of(
                [
                    outcome,
                    "把东西交到他手上，说一句短的，让他接着。不要描述你刚才是怎么做的。",
                ],
                kind="delivery",
            )

        # greet / observe：不带事实，但要走"主动开口"的任务分支，
        # 否则她会像在答问题，而不是走过来打个招呼。
        return Facts.of([], kind="greet")

    def _convo_context(self, npc_id: str) -> tuple[tuple[Turn, ...], str]:
        """当前会话的（历史, 摘要）。没有打开中的会话时返回空。"""
        sess = self.session
        if sess and not sess.is_closed and sess.npc_id == npc_id:
            return (
                tuple(sess.transcript()),
                sess.summary.text if sess.summary else "",
            )
        return (), ""

    def _recent_turns(self, npc_id: str) -> tuple[Turn, ...]:
        """整段对话都给出去，截多少条由 Stylist 决定。"""
        return self._convo_context(npc_id)[0]

    def _find_memory(self, npc_id: str, memory_id: str | None):
        for m in self.deps.store.items(npc_id):
            if m.id == memory_id:
                return m
        return None

    def shake_memory(self, npc_id: str) -> Facts | None:
        """SHARE_MEMORY 的开局素材：把她待办里的那条约定拿出来。

        注意这不算"玩家输入"，所以不写进 L0 的 player 轮次。
        """
        items = self.deps.retriever.unresolved(self.profile.player_id, npc_id)
        if not items:
            return None
        return Facts.of([f"你还记得：{items[0].value}"], provenance=f"memory:{items[0].id}")

    # ── 调试（演示必需） ──

    def debug_set_affection(self, npc_id: str, value: int) -> None:
        self.deps.affection.debug_set(self.profile, npc_id, value)
        self.deps.profiles.save(self.profile)

    def debug_advance(self, **kwargs) -> datetime:
        if not hasattr(self.deps.clock, "advance"):
            raise RuntimeError("当前时钟不支持手动推进")
        return self.deps.clock.advance(**kwargs)  # type: ignore[attr-defined]

    def debug_dump(self) -> dict[str, object]:
        out: dict[str, object] = {
            "player": self.profile.player_id,
            "affection": {
                k: {"affinity": v.affinity, "stage": v.stage.value}
                for k, v in self.profile.affection.items()
            },
        }
        if self.session:
            out["session"] = self.session.stats()
        out["behaviors"] = self.behavior.snapshot()
        return out
