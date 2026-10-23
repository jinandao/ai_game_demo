"""UI：主窗口与游戏循环。

一屏一场景，点击移动、点建筑切换场景，对话界面替换整个场景。
LLM 调用走线程池，主循环只取结果。
"""

from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pygame

from ..action import services
from ..behavior import SpeakRequest
from ..domain.enums import ActionId, Stage
from ..game import GameApp
from . import theme as T
from .assets import Assets
from .chat_panel import ChatPanel
from .renderer import SCREEN_H, SCREEN_W, SceneRenderer

PROJECT_ROOT = Path(__file__).resolve().parents[2]

IDLE_EVERY = 20.0        # 秒：多久让 NPC 自己决定一次
PLAYER_SPEED = 240.0     # px/s
ENTER_RANGE = 46         # 走到门口多近算"到了"

STAGE_RANK = {
    Stage.STRANGER: 0,
    Stage.ACQUAINTANCE: 1,
    Stage.CLOSE: 2,
    Stage.AMBIGUOUS: 3,
    Stage.DEPENDENT: 4,
}

# 场景的上一层。ESC 和"出口"都按这张表返回
PARENT_SCENE = {
    "workshop": "outdoor",
    "canteen": "outdoor",
    "lan_room": "outdoor",
    "mio_room": "workshop",
}

# 好感不足时的婉拒台词（按档位取，保证低好感也礼貌）
REFUSALS = {
    "A": {
        0: "那边你不能进。",
        1: "急什么。",
    },
    "B": {
        0: "里面有点乱……不太方便呢。",
        1: "等我把这儿收拾一下，好吗。",
    },
}


class Worker:
    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="llm")
        self._q: queue.Queue[tuple[str, str, object]] = queue.Queue()

    def submit(self, tag: str, fn, *args, **kwargs) -> None:
        self._pool.submit(self._wrap, tag, fn, args, kwargs)

    def push(self, tag: str, value: object) -> None:
        """任务执行中追加中间结果。流式对话用它一块一块往回送。"""
        self._q.put((tag, "ok", value))

    def _wrap(self, tag: str, fn, args, kwargs) -> None:
        try:
            self._q.put((tag, "ok", fn(*args, **kwargs)))
        except Exception as e:  # noqa: BLE001
            self._q.put((tag, "err", f"{type(e).__name__}: {e}"))

    def drain(self) -> list[tuple[str, str, object]]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class AppWindow:
    def __init__(self, app: GameApp) -> None:
        pygame.init()
        pygame.display.set_caption("残响都市 · demo")

        self.app = app
        self.fonts = T.Fonts()
        self.worker = Worker()
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))

        self.names = {npc_id: p.name for npc_id, p in app.deps.personas.items()}
        self.assets = Assets(PROJECT_ROOT)
        self.renderer = SceneRenderer(app.deps.world, self.fonts, self.names, self.assets)
        self.panel = ChatPanel(self.fonts, self.assets)

        # 玩家状态
        self.player_scene = "outdoor"
        self.player_xy = [720.0, 660.0]
        self.player_target: tuple[float, float] | None = None
        self.pending_enter: str | None = None
        app.behavior.move_player(self.player_scene, tuple(self.player_xy))

        self.running = True
        self.debug_open = False
        self.debug_npc = "A"
        self._debug_hits: dict[str, pygame.Rect] = {}

        self.toast: tuple[str, float] | None = None
        self._idle_timer = 2.0          # 别等 20 秒，进来就让人开口
        self._chat_busy = False
        self._speaking: set[str] = set()
        self._close_deadline = 0.0      # 结束会话的超时兜底

        # NPC 主动开口 → 弹一张"想和你说话"的卡片，点了才进对话
        self.pending_say: dict[str, tuple[str, float]] = {}
        self._say_hits: dict[str, pygame.Rect] = {}
        # 她刚提出、等玩家答应的事：{npc_id: action_id}。
        # 对话里只在她提过之后才显示"答应"按钮，否则什么都没有。
        self.pending_offer: dict[str, str] = {}
        # 她带路的时候，玩家跟着她走，手动移动暂时失效
        self.following: str | None = None

        app.speech_hook = self._async_speak

        # 开场：先黑屏发一条请求，等模型真的回话了再进游戏。
        # 本地模型冷启要加载权重，把这段等待放在黑屏里，而不是让人对着空场景猜。
        self.loading = True
        self.load_msg = "正在唤醒模型"
        self.load_t = 0.0
        self.worker.submit("warmup", self.app.warmup)

    # ── 主循环 ──

    def run(self) -> None:
        clock = pygame.time.Clock()
        while self.running:
            dt = min(0.1, clock.tick(60) / 1000.0)
            for event in pygame.event.get():
                self._on_event(event)
            self._poll()
            self._update(dt)
            self._draw()
            pygame.display.flip()

        self.worker.shutdown()
        self.app.shutdown()      # 收尾未结束的会话 + 把记忆数组写回文件
        pygame.quit()

    # ── 事件 ──

    def _on_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.running = False
            return

        if self.loading:
            # 开场等待期间只接受关闭窗口
            return

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_F1:
                self.debug_open = not self.debug_open
                return
            if event.key == pygame.K_F2:
                self._advance_time()
                return
            if event.key == pygame.K_ESCAPE and not self.panel.is_open:
                back = PARENT_SCENE.get(self.player_scene)
                if back:
                    self._change_scene(back)      # 室内按 ESC 回上一层
                else:
                    self.running = False          # 在户外才真的退出游戏
                return

        # 对话界面独占输入
        if self.panel.is_open:
            action = self.panel.handle_event(event)
            if action:
                self._handle_chat_action(action)
            return

        if self.debug_open and event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self._handle_debug_click(event.pos):
                return

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            # 主动搭话的卡片优先于其它点击
            for npc_id, box in self._say_hits.items():
                if box.collidepoint(event.pos):
                    opening = self.pending_say.get(npc_id, ("", 0.0))[0]
                    self.pending_say.pop(npc_id, None)
                    self._open_chat(npc_id, opening=opening)
                    return
            if self._hit_gear(event.pos):
                self.debug_open = not self.debug_open
                return
            npc_id = self.renderer.npc_at(event.pos)
            if npc_id:
                self._open_chat(npc_id)
                return
            prop = self.renderer.prop_at(event.pos)
            if prop:
                self._interact(prop)
                return
            # 点空地：走过去（跟着她走的时候不受理，免得一乱点就跑偏）
            if self.following is None:
                self._set_target(float(event.pos[0]), float(event.pos[1]))
                self.pending_enter = None

    def _set_target(self, x: float, y: float) -> None:
        """设定玩家目标点并夹到可行走范围内，否则目标会落在边界外。"""
        self.player_target = (
            max(20.0, min(SCREEN_W - 20.0, x)),
            max(260.0, min(SCREEN_H - 24.0, y)),
        )

    def _handle_chat_action(self, action: tuple) -> None:
        kind = action[0]
        if kind == "end":
            self._close_chat()
        elif kind == "send":
            self._send(action[1])
        elif kind == "action":
            npc_id = self.panel.npc_id
            if not npc_id:
                return
            action_id = str(action[1])
            self.pending_offer.pop(npc_id, None)      # 已经答过了，按钮收起来
            if services.get(action_id):
                # 照料动作：她先问过，这是玩家在确认。
                # 确认后就关对话——她要去做了，两个人不能还隔着面板说话。
                self.app.accept_service(npc_id, action_id)
                self._close_chat()
                self._toast("她说等着，然后就走了", seconds=1.8)
                return
            self.app.accept_invite(npc_id, _as_action_id(action_id))
            self._send(self._action_sentence(action_id))
            self._refresh_actions()

    # ── 交互 ──

    def _interact(self, prop) -> None:
        if prop.interact == "enter":
            self._try_enter(prop)
            return
        if prop.interact == "sit":
            self._set_target(prop.x + prop.w / 2, prop.y + prop.h + 20)
            self._toast(prop.text or f"你在{prop.name}边坐下")
            return
        if prop.text:
            self._toast(prop.text)

    def _try_enter(self, prop) -> None:
        target = prop.target
        if not target:
            return

        if target in ("lan_room", "mio_room"):
            owner = "A" if target == "lan_room" else "B"
            state = self.app.profile.affection.get(owner)
            rank = STAGE_RANK.get(state.stage, 0) if state else 0
            if rank < STAGE_RANK[Stage.CLOSE]:
                agent = self.app.behavior.agent(owner)
                if agent.place == target:
                    # 她在屋里：当面婉拒，但不切场景
                    line = REFUSALS.get(owner, {}).get(min(rank, 1), "现在不行。")
                    self.renderer.say(owner, f"（门后）{line}", hold=4)
                    self._toast("门没有开")
                else:
                    self._toast("门锁着")
                return

        door = self.app.deps.world.door(self.player_scene, target)
        self._set_target(float(door[0]), float(door[1]))
        self.pending_enter = target

    def _change_scene(self, scene_id: str, from_scene: str | None = None) -> None:
        from_scene = from_scene or self.player_scene
        world = self.app.deps.world

        # 回到户外时，出现在刚才那栋建筑的门口，而不是场景出生点
        spawn = world.entry(scene_id)
        if scene_id == "outdoor" and from_scene != "outdoor":
            door = world.scene("outdoor").doors.get(from_scene)
            if door is not None:
                spawn = (door[0], door[1] + 36)

        self.player_scene = scene_id
        self.pending_say.clear()          # 换场景后旧的"想和你说话"作废
        self.player_xy = [float(spawn[0]), float(spawn[1])]
        self.player_target = None
        self.pending_enter = None
        self.app.behavior.move_player(scene_id, (self.player_xy[0], self.player_xy[1]))
        self._toast(world.scene(scene_id).name, seconds=1.2)

    # ── 会话 ──

    def _open_chat(self, npc_id: str, opening: str | None = None) -> None:
        if self._chat_busy:
            return

        # 玩家可能点的是"想和你说话"卡片，也可能直接点人。
        # 后者没带 opening，这里从 pending_say 里补一次——否则她主动开的口就白说了。
        if opening is None:
            opening = self.pending_say.pop(npc_id, ("", 0.0))[0] or None
        else:
            self.pending_say.pop(npc_id, None)

        # opening 会作为第 0 轮写进 transcript，她的回答才接得上自己刚说的话
        self.app.enter_chat(npc_id, opening=opening)
        self.panel.open(npc_id, self.names.get(npc_id, npc_id))
        if opening:
            self.panel.add("npc", opening)
        self._refresh_actions()

    def _send(self, text: str) -> None:
        if not text.strip() or self._chat_busy:
            return
        self.panel.add("player", text)
        self.panel.set_busy(True)
        self.panel.begin_stream("npc")     # 先占一个气泡，边生成边填
        self._chat_busy = True
        self.worker.submit("reply", self.app.send, text, self._on_delta)

    def _on_delta(self, chunk: str) -> None:
        """在 worker 线程里被调用。只往队列里塞，不碰 UI。"""
        self.worker.push("delta", chunk)

    def _close_chat(self) -> None:
        if self._chat_busy or not self.panel.is_open:
            return
        self._chat_busy = True
        # 收尾要跑一次完整抽取，本地 7B 生成几百 token 要几十秒。
        # UI 不能陪等：先关面板，抽取在后台跑完再落盘。
        self._close_deadline = time.monotonic() + 120.0
        self.panel.close()
        self._idle_timer = 1.5
        self._toast("她在收尾，稍等一下…")
        self.worker.submit("end", self.app.close_chat)

    def _async_speak(self, req: SpeakRequest) -> str:
        if req.npc_id in self._speaking:
            return ""
        self._speaking.add(req.npc_id)
        self.worker.submit("speech", self._speak_task, req)
        return ""

    def _speak_task(self, req: SpeakRequest) -> tuple[str, str]:
        return req.npc_id, self.app.speak_sync(req)

    def _refresh_actions(self) -> None:
        """对话下方的按钮，只在她提出之后出现。"""
        npc_id = self.panel.npc_id
        if not npc_id:
            self.panel.set_actions([])
            return

        action_id = self.pending_offer.get(npc_id, "")
        if not action_id:
            self.panel.set_actions([])      # 她没说，就不给按钮
            return
        self.panel.set_actions([(action_id, self._accept_label(action_id))])

    @staticmethod
    def _accept_label(action_id: str) -> str:
        if services.get(action_id):
            return "好，麻烦你了"
        return {
            "invite_walk": "好啊，一起走走",
            "invite_visit": "行，去你那儿坐会儿",
            "request_hug": "过来吧",
        }.get(action_id, "好啊")

    def _action_sentence(self, action_id: str) -> str:
        if services.get(str(action_id)):
            return "好啊，麻烦你了。"
        return {
            "invite_walk": "好啊，一起走走吧。",
            "invite_visit": "行，去你那儿坐会儿。",
            "request_hug": "过来吧。",
        }.get(str(action_id), "好。")

    # ── 主动搭话 ──

    def _offer_chat(self, npc_id: str, text: str) -> None:
        """她主动开口时，弹一张可点的卡片。

        只在她和你同一个场景时才弹——不然会出现"隔着两栋房子喊你说话"。
        """
        if self.panel.is_open or self._chat_busy or not text.strip():
            return
        agent = self.app.behavior.agent(npc_id)
        if agent.place != self.player_scene:
            return
        self.pending_say[npc_id] = (text.strip(), time.monotonic() + 20.0)
        if len(self.pending_say) > 2:
            oldest = min(self.pending_say, key=lambda k: self.pending_say[k][1])
            self.pending_say.pop(oldest, None)

    # ── 结果回收 ──

    def _poll(self) -> None:
        for tag, status, value in self.worker.drain():
            if status == "err":
                if self.loading and tag == "warmup":
                    # 模型没起来也放人进去：能看到界面、能走能点，只是她不会说话
                    self.loading = False
                    self._toast("模型没响应，以离线状态进入（NPC 不会说话）", seconds=4.0)
                    continue
                self._close_deadline = 0.0
                self._chat_busy = False
                self.panel.set_busy(False)
                self.panel.end_stream(None)     # 清掉流式留下的空气泡
                # 结束失败也必须让玩家从对话里退出来，否则界面会卡死在面板上
                if self.panel.is_open:
                    self.panel.close()
                self._toast(f"出错了：{value}")
                continue

            if tag == "warmup":
                self.loading = False
                self._toast(f"模型就绪（{value}）")
                continue

            if tag == "delta":
                self.panel.append_stream(str(value))
                continue

            if tag == "reply":
                self._chat_busy = False
                self.panel.set_busy(False)
                text = getattr(value, "text", str(value))
                self.panel.end_stream(text)     # 用清洗后的最终文本覆盖一次
                self._refresh_actions()

                # 她说要带路：关对话，两个人一起走过去
                goto = self.app.deps.stylist.last_goto
                if goto and self.panel.npc_id:
                    self._start_follow(self.panel.npc_id, goto)

            elif tag == "end":
                self._close_deadline = 0.0
                self._chat_busy = False
                self.panel.set_busy(False)
                self._idle_timer = 1.5     # 聊完别让她愣 20 秒才想起自己该干嘛
                report = value
                self.panel.close()
                if report is not None:
                    self._toast(
                        f"会话结束：{report.turns} 轮，她记住了 {report.facts_written} 件事"
                    )
                else:
                    self._toast("会话结束")

            elif tag == "speech":
                npc_id, text = value  # type: ignore[misc]
                self._speaking.discard(npc_id)
                if text.strip():
                    self.renderer.say(npc_id, text)
                    self._offer_chat(npc_id, text)

    # ── 更新 ──

    def _start_follow(self, npc_id: str, scene_id: str) -> None:
        """她说"跟上"，那就真的跟上：关对话，她走前面，玩家跟在后面。"""
        self._close_chat()
        self.following = npc_id
        self.app.walk_to(npc_id, scene_id)
        self._toast("跟上去", seconds=1.5)

    def _update_follow(self) -> None:
        if self.following is None:
            return

        try:
            agent = self.app.behavior.agent(self.following)
        except KeyError:
            self.following = None
            return

        # 她换场景了，玩家跟着一起过去
        if agent.place != self.player_scene:
            self._change_scene(agent.place, from_scene=self.player_scene)
            return

        if agent.walking:
            self._set_target(agent.x + 36, agent.y + 26)
            return

        dx = agent.x - self.player_xy[0]
        dy = agent.y - self.player_xy[1]
        if (dx * dx + dy * dy) ** 0.5 < 90:
            self.following = None
            self._toast("到了")
        else:
            self._set_target(agent.x + 32, agent.y + 22)

    def _update(self, dt: float) -> None:
        if self.loading:
            self.load_t += dt
            return

        self._update_follow()
        self._update_player(dt)

        # 决策：定时入队 → 队列自己攒批、串行推理 → 结果在 UI 线程生效
        self._idle_timer -= dt
        if self._idle_timer <= 0:
            self._idle_timer = IDLE_EVERY
            self.app.idle_tick()      # 只写待办表，立刻返回
        self.app.deps.decisions.tick()
        self.app.drain_decisions()

        self.app.tick(dt)

        # 收尾太久说明模型那边卡住了。面板已经关了，这里只提示一下
        if self._close_deadline and time.monotonic() > self._close_deadline:
            self._close_deadline = 0.0
            self._chat_busy = False
            self.panel.set_busy(False)
            self._toast("收尾超时了，这段对话的记忆可能没存上", seconds=4.0)

        for event in self.app.drain_events():
            self.renderer.on_event(event.kind, event.npc_id, event.payload)

            # 只在她提出之后才给按钮。
            if event.kind in ("offer", "invite"):
                action = str(event.payload.get("action") or "")
                if action:
                    self.pending_offer[event.npc_id] = action

            if event.kind in ("say", "invite"):
                text = (event.payload.get("text") or "").strip()
                if not text:
                    continue
                if self.panel.is_open:
                    if event.npc_id == self.panel.npc_id:
                        self.panel.add("npc", text)
                else:
                    self._offer_chat(event.npc_id, text)

    def _update_player(self, dt: float) -> None:
        target = self.player_target
        if target is None:
            return
        dx, dy = target[0] - self.player_xy[0], target[1] - self.player_xy[1]
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= 5:
            self.player_xy = [target[0], target[1]]
            self.player_target = None
            self.app.behavior.move_player(self.player_scene, (target[0], target[1]))
            if self.pending_enter:
                scene = self.pending_enter
                self.pending_enter = None
                self._change_scene(scene)
            return

        step = min(dist, PLAYER_SPEED * dt)
        self.player_xy[0] += dx / dist * step
        self.player_xy[1] += dy / dist * step
        self.player_xy[0] = max(20, min(SCREEN_W - 20, self.player_xy[0]))
        self.player_xy[1] = max(260, min(SCREEN_H - 20, self.player_xy[1]))
        # 直接写行为层的玩家状态，不走 move_player（那会每帧发一次事件）
        self.app.behavior.player_place = self.player_scene
        self.app.behavior.player_xy = (self.player_xy[0], self.player_xy[1])

    def _advance_time(self) -> None:
        try:
            self.app.debug_advance(hours=1)
            self._toast("时间过去了一小时")
            self._idle_timer = 0.0
        except RuntimeError:
            self._toast("当前使用系统时钟，时间会自己走")

    # ── 绘制 ──

    def _draw(self) -> None:
        if self.loading:
            self._draw_loading()
            return

        if self.panel.is_open:
            self.panel.draw(self.screen)
            self._draw_toast()
            return

        self.renderer.draw(
            self.screen,
            scene_id=self.player_scene,
            agents=self.app.behavior.agents(),
            player_xy=(self.player_xy[0], self.player_xy[1]),
            player_name=self.app.profile.name,
        )
        self._draw_topbar()
        self._draw_hint()
        self._draw_pending_say()
        self._draw_gear()
        self._draw_toast()
        if self.debug_open:
            self._draw_debug()

    def _draw_loading(self) -> None:
        """开场黑屏。等模型回第一条消息——顺便把权重加载的时间盖掉。"""
        self.screen.fill((6, 7, 9))

        title = self.fonts.get(34, bold=True).render("残响都市", True, T.TEXT)
        self.screen.blit(
            title, title.get_rect(center=(SCREEN_W // 2, SCREEN_H // 2 - 48))
        )

        dots = "·" * (1 + int(self.load_t * 2) % 4)
        sub = self.fonts.get(19).render(f"{self.load_msg}{dots}", True, T.TEXT_DIM)
        self.screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, SCREEN_H // 2 + 14)))

        if self.load_t > 6:
            tip = self.fonts.get(15).render(
                "本地模型第一次推理要加载权重，慢一点是正常的。",
                True,
                T.TEXT_FAINT,
            )
            self.screen.blit(
                tip, tip.get_rect(center=(SCREEN_W // 2, SCREEN_H // 2 + 52))
            )

    def _draw_topbar(self) -> None:
        bar = pygame.Surface((SCREEN_W, 40), pygame.SRCALPHA)
        pygame.draw.rect(bar, (12, 14, 18, 130), bar.get_rect())
        self.screen.blit(bar, (0, 0))

        scene = self.app.deps.world.scene(self.player_scene)
        name = self.fonts.get(18, bold=True).render(scene.name, True, T.TEXT)
        self.screen.blit(name, (24, 10))

        now = self.app.deps.clock.now()
        clock_text = f"{now.strftime('%H:%M')} · 云塌后第 19 年"
        if self.player_scene != "outdoor":
            clock_text = "ESC 返回 · " + clock_text
        info = self.fonts.get(15).render(clock_text, True, T.TEXT_DIM)
        self.screen.blit(info, (SCREEN_W - info.get_width() - 24, 13))

    def _draw_hint(self) -> None:
        near = self.renderer.npc_at(pygame.mouse.get_pos())
        if not near:
            return
        text = f"点击和{self.names.get(near, near)}说话"
        label = self.fonts.get(16).render(text, True, T.TEXT)
        box = pygame.Rect(SCREEN_W - label.get_width() - 60, SCREEN_H - 90, label.get_width() + 36, 40)
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.rect(bg, (18, 21, 26, 220), bg.get_rect(), border_radius=8)
        pygame.draw.rect(bg, (*T.PANEL_LINE, 255), bg.get_rect(), 1, border_radius=8)
        self.screen.blit(bg, box.topleft)
        self.screen.blit(label, (box.left + 18, box.top + 11))

    def _draw_pending_say(self) -> None:
        """她主动打招呼时弹的卡片。点一下才进对话，第一句就是她说的话。"""
        self._say_hits = {}
        if not self.pending_say:
            return

        now = time.monotonic()
        name_font = self.fonts.get(17, bold=True)
        tip_font = self.fonts.get(14)
        y = SCREEN_H - 210

        for npc_id, (text, expire) in list(self.pending_say.items()):
            agent = self.app.behavior.agent(npc_id)
            if now > expire or agent.place != self.player_scene:
                self.pending_say.pop(npc_id, None)
                continue

            box = pygame.Rect(SCREEN_W - 400, y, 370, 80)
            bg = pygame.Surface(box.size, pygame.SRCALPHA)
            pygame.draw.rect(bg, (16, 19, 24, 235), bg.get_rect(), border_radius=10)
            pygame.draw.rect(bg, (*T.ACCENT_DIM, 255), bg.get_rect(), 1, border_radius=10)
            self.screen.blit(bg, box.topleft)

            name = name_font.render(self.names.get(npc_id, npc_id), True, T.ACCENT)
            self.screen.blit(name, (box.left + 16, box.top + 11))

            tip = tip_font.render("想和你说话 · 点击进入对话", True, T.TEXT_DIM)
            self.screen.blit(tip, (box.left + 16 + name.get_width() + 12, box.top + 14))

            preview = tip_font.render(_ellipsis(text, 22), True, T.TEXT)
            self.screen.blit(preview, (box.left + 16, box.top + 46))

            self._say_hits[npc_id] = box
            y -= 90

    def _draw_gear(self) -> None:
        box = pygame.Rect(1398, 758, 30, 30)
        gear = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.circle(gear, (220, 220, 220, 50), (15, 15), 9)
        pygame.draw.circle(gear, (220, 220, 220, 20), (15, 15), 13, 2)
        self.screen.blit(gear, box.topleft)

    def _hit_gear(self, pos) -> bool:
        return pygame.Rect(1398, 758, 30, 30).collidepoint(pos)

    def _draw_toast(self) -> None:
        if not self.toast:
            return
        text, until = self.toast
        if pygame.time.get_ticks() / 1000 > until:
            self.toast = None
            return

        font = self.fonts.get(17)
        surface = font.render(text, True, T.TEXT)
        box = pygame.Rect(0, 0, surface.get_width() + 36, 42)
        box.center = (SCREEN_W // 2, SCREEN_H - 120)
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.rect(bg, (18, 21, 26, 230), bg.get_rect(), border_radius=10)
        pygame.draw.rect(bg, (*T.PANEL_LINE, 255), bg.get_rect(), 1, border_radius=10)
        self.screen.blit(bg, box.topleft)
        self.screen.blit(surface, (box.left + 18, box.top + 10))

    def _toast(self, text: str, seconds: float = 3.2) -> None:
        self.toast = (text, pygame.time.get_ticks() / 1000 + seconds)

    # ── 调试面板 ──

    def _draw_debug(self) -> None:
        box = pygame.Rect(980, 400, 440, 380)
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.rect(bg, (10, 12, 16, 235), bg.get_rect(), border_radius=8)
        pygame.draw.rect(bg, (*T.PANEL_LINE, 255), bg.get_rect(), 1, border_radius=8)
        self.screen.blit(bg, box.topleft)

        self._debug_hits = {}
        title = self.fonts.get(16, bold=True).render("调试", True, T.ACCENT)
        self.screen.blit(title, (box.left + 16, box.top + 12))

        y = box.top + 48
        font = self.fonts.get(15)
        for npc_id, p in self.app.deps.personas.items():
            state = self.app.profile.affection.get(npc_id)
            aff = state.affinity if state else 0
            stage = state.stage.value if state else "-"
            line = f"{p.name}   好感 {aff} / {stage}"
            self.screen.blit(font.render(line, True, T.TEXT_DIM), (box.left + 16, y))
            y += 26

        y += 8
        self.screen.blit(self.fonts.get(14).render("对象", True, T.TEXT_FAINT), (box.left + 16, y))
        x = box.left + 70
        for npc_id, p in self.app.deps.personas.items():
            btn = pygame.Rect(x, y - 4, 64, 26)
            active = self.debug_npc == npc_id
            pygame.draw.rect(self.screen, (52, 58, 48) if active else (30, 33, 39), btn, border_radius=5)
            pygame.draw.rect(self.screen, T.PANEL_LINE, btn, 1, border_radius=5)
            label = self.fonts.get(14).render(p.name, True, T.TEXT if active else T.TEXT_DIM)
            self.screen.blit(label, (btn.centerx - label.get_width() // 2, btn.centery - 8))
            self._debug_hits[f"npc:{npc_id}"] = btn
            x += 72

        y += 40
        self.screen.blit(
            self.fonts.get(14).render("一键设定好感", True, T.TEXT_FAINT), (box.left + 16, y)
        )
        y += 26
        x = box.left + 16
        for value in (0, 10, 25, 40, 60, 80):
            btn = pygame.Rect(x, y, 58, 30)
            pygame.draw.rect(self.screen, (34, 38, 46), btn, border_radius=5)
            pygame.draw.rect(self.screen, T.ACCENT_DIM, btn, 1, border_radius=5)
            label = self.fonts.get(14).render(str(value), True, T.ACCENT)
            self.screen.blit(label, (btn.centerx - label.get_width() // 2, btn.centery - 8))
            self._debug_hits[f"aff:{value}"] = btn
            x += 64

        y += 48
        agent = self.app.behavior.agent(self.debug_npc)
        runner = self.app.behavior.runner(self.debug_npc)
        lines = [
            f"场景 {self.player_scene}",
            f"{self.debug_npc} {runner.label or '-'} @ {agent.place} ({int(agent.x)},{int(agent.y)})",
        ]
        session = self.app.debug_dump().get("session")
        if session:
            lines.append(
                f"会话 {session['turns']} 轮 · token≈{session['est_tokens']}"
                f"/{session['t_compact']} · 摘要 L{session['summary_level']}"
            )
        route = self.app.deps.pipeline.last_route
        if route:
            lines.append(f"上一轮意图 {route.intent.value}")

        q = self.app.deps.decisions.stats()
        q_state = "暂停" if q["paused"] else ("推理中" if q["busy"] else "空闲")
        fails = self.app.deps.planner.stats()["failures"]
        lines.append(
            f"决策队列 {q_state}  待{int(q['pending'])}  批{int(q['runs'])}/{int(q['last_batch'])}"
            f"  失败{fails}"
        )

        err = self.app.deps.stylist.last_error
        if err:
            lines.append(f"上次聊天失败：{err}")

        q = self.app.deps.decisions.stats()
        state = "暂停" if q["paused"] else ("推理中" if q["busy"] else "空闲")
        lines.append(f"决策队列 {state}  待{int(q['pending'])}  批{int(q['runs'])}/{int(q['last_batch'])}")

        for line in lines:
            self.screen.blit(
                self.fonts.get(14).render(line, True, T.TEXT_FAINT), (box.left + 16, y)
            )
            y += 22

    def _handle_debug_click(self, pos) -> bool:
        for key, box in self._debug_hits.items():
            if not box.collidepoint(pos):
                continue
            if key.startswith("npc:"):
                self.debug_npc = key[4:]
                return True
            if key.startswith("aff:"):
                value = int(key[4:])
                self.app.debug_set_affection(self.debug_npc, value)
                self._toast(f"好感设为 {value}")
                return True
        return False


def _ellipsis(text: str, limit: int) -> str:
    flat = text.replace("\n", " ")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _as_action_id(raw):
    try:
        return ActionId(raw)
    except ValueError:
        return ActionId.INVITE_WALK
