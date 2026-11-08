"""主窗口与游戏循环。"""

from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pygame

from ..action import services
from ..behavior import SpeakRequest
from ..domain.enums import ActionId, Stage
from ..domain.models import AffectionState
from ..core import GameApp
from ..game.catalog import ITEMS, MONSTERS, SHOP, recipes_at
from ..game.combat import crit_for_stage
from ..game.encounter import AREAS, Encounter
from ..game.hub import Hub
from ..game.state import SLOTS_PER_DAY
from ..game.story import load_chapter
from . import theme as T
from .assets import Assets
from .battle_view import BattleView
from .chat_panel import ChatPanel
from .panels import Panel, make_panel
from .renderer import SCREEN_H, SCREEN_W, SceneRenderer
from .story_view import StoryView

PROJECT_ROOT = Path(__file__).resolve().parents[2]

IDLE_EVERY = 20.0        # 秒：多久让 NPC 自己决定一次
PLAYER_SPEED = 280.0     # px/s。世界放大 4 倍后从 240 提上来，横穿空地约 10 秒
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
    "kitchen": "canteen",
    "storage": "outdoor",
    "hall": "outdoor",
    "garden": "outdoor",
    "ruins": "outdoor",
    "lan_room": "outdoor",
    "mio_room": "workshop",
}

# 这些场景里有工作台，进去就能做东西
CRAFT_STATION = {
    "kitchen": "kitchen",
    "storage": "storage",
    "workshop": "workshop",
}

def _battle_spot(world, scene_id: str, zone: str = "") -> tuple[int, int]:
    """剧情战斗把人带到哪个点。"""
    zones = AREAS.get(scene_id)
    if zones:
        box = zones.get(zone) if zone else None
        if box is None:
            box = next(iter(zones.values()))
        x, y, w, h = box
        return x + w // 2, y + h // 2
    scene = world.scene(scene_id)
    return scene.entry[0], max(scene.walk_top + 160, scene.entry[1] - 280)

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

        # 一旦场景变大或者布局挪了，人就会出生在墙里。
        self.player_scene = "outdoor"
        spawn = app.deps.world.entry(self.player_scene)
        self.player_xy = [float(spawn[0]), float(spawn[1])]
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
        # 顶部栏的剧情按钮命中框。每帧在 _draw_story_button 里更新
        self._story_btn: pygame.Rect | None = None
        # 她刚提出、等玩家答应的事：{npc_id: action_id}。
        self.pending_offer: dict[str, str] = {}
        # 她带路的时候，玩家跟着她走，手动移动暂时失效
        self.following: str | None = None

        # 玩法层：Hub 管住 状态 / 任务 / 商店 / 遭遇 / 存档。
        self.hub = Hub(
            save_path=PROJECT_ROOT / "data" / "save" / f"{app.profile.player_id}.json"
        )
        self.hub.controller.on_affinity = self._story_affinity
        self.hub.controller.on_memory = app.remember_quest
        # 剧情事件 do="mood" → 改这个 NPC 的心情（落盘在 core.set_mood）
        self.hub.controller.on_mood = app.set_mood
        # 玩家在剧情里选的分支 → 写进在场 NPC 的记忆（文本在 story/memories.json）
        self.hub.controller.on_story_choice = app.remember_story_choice
        self.hub.controller.crit_provider = self._npc_crit
        self.hub.controller.npc_names = dict(self.names)   # 写记忆时用名字，不用 id
        self.hub.profile_dump = self._dump_profile
        self.hub.profile_load = self._load_profile
        # 她的执行进度也要进存档：读档时"正做到第几条动作"得接得上
        self.hub.plans_dump = app.behavior.plans_snapshot
        self.hub.load()                       # 没档就按新游戏跑
        self.hub.controller.ensure_party()
        # 执行进度在这里恢复（此时 NPC 已经 spawn 过了）
        app.behavior.plans_restore(self.hub.loaded_plans or {})

        # 读档后玩家得站在存档里的那个场景，不是永远回空地。
        saved_place = self.hub.controller.player.place
        if saved_place and app.deps.world.has(saved_place):
            self.player_scene = saved_place
            here = app.deps.world.entry(saved_place)
            self.player_xy = [float(here[0]), float(here[1])]
            app.behavior.move_player(saved_place, tuple(self.player_xy))

        self.hub.sync_chapter(max(1, self.hub.story_chapter))
        self.hub.enter_scene(self.player_scene)
        self.renderer.snap(
            app.deps.world.scene(self.player_scene), tuple(self.player_xy)
        )

        self.story_view: StoryView | None = None
        self.battle_view: BattleView | None = None
        self.panel_view: Panel | None = None
        # 剧情战斗节拍：{"scene","spot","enemies","solo","phase","t"}，不在节拍里就是 None
        self._battle_beat: dict | None = None
        self._pending_sidequest = ""          # 正在播的支线 id（播完结算）
        self._story_file_name = ""            # 正在播的剧本文件名，存档要用
        self._reset_armed_at = 0.0            # F8 第一次按的时间（两次确认）

        app.speech_hook = self._async_speak

        # 决策层要知道血量、票据、背包、货架、能做哪些菜、剧情走到哪
        app.game_view_provider = self._game_view

        # 存档里有没播完的剧情，直接从那一句接着播
        self._resume_story()

        # 开场：先黑屏发一条请求，等模型真的回话了再进游戏。
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
        self._sync_story_cursor()   # 剧情播到一半退出，也要记住位置
        self.hub.save()             # 玩法进度：血、钱、背包、任务、天数、NPC 状态
        self.app.shutdown()         # 收尾未结束的会话 + 把记忆数组写回文件
        pygame.quit()

    # ── 事件 ──

    def _on_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.running = False
            return

        if self.loading:
            # 开场等待期间只接受关闭窗口
            return

        # 剧情战斗的走位阶段：人在往战场走，这时候不接受任何操作。
        if self._battle_beat is not None and self.battle_view is None:
            return

        # 战斗演出：点一下快进，再点一下关掉
        if self.battle_view is not None:
            if self.battle_view.handle(event) and self.battle_view.done:
                self._close_battle()
            return

        # 弹窗面板：Esc 关，其余交给它
        if self.panel_view is not None:
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                self._close_panel()
                return
            self.panel_view.handle(event)
            return

        # 剧情独占输入：先让它吃，吃不下也不往下传
        if self.story_view is not None:
            if self.story_view.done:
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self._finish_story()
                elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_SPACE, pygame.K_RETURN
                ):
                    self._finish_story()
                return
            if self.story_view.handle(event):
                return
            # StoryView 故意不拦 ESC（它只管推进剧情）。但要是让 ESC 漏到下面
            return

        # 对话面板独占输入：必须放在功能键之前。
        if self.panel.is_open:
            action = self.panel.handle_event(event)
            if action:
                self._handle_chat_action(action)
            return

        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_F3:
                self._start_story()
                return
            if event.key == pygame.K_b:
                self._open_panel("bag")
                return
            if event.key == pygame.K_q:
                self._open_panel("quest")
                return
            if event.key == pygame.K_F5:
                self._toast("已存档" if self.hub.save() else "存不了：没有存档路径")
                return
            if event.key == pygame.K_F9:
                self._load_game()
                return
            if event.key == pygame.K_F8:
                self._reset_progress()
                return
            if event.key == pygame.K_F1:
                self.debug_open = not self.debug_open
                return
            if event.key == pygame.K_F2:
                self._advance_time()
                return
            if event.key == pygame.K_F4:
                # 意图来源：规则 ↔ LLM。同一个存档上直接对比，不用重启。
                self.app.deps.planner.toggle_intent_mode()
                return
            if event.key == pygame.K_ESCAPE and not self.panel.is_open:
                back = PARENT_SCENE.get(self.player_scene)
                if back:
                    self._change_scene(back)      # 室内按 ESC 回上一层
                else:
                    self.running = False          # 在户外才真的退出游戏
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
                    if self._start_sidequest(npc_id):
                        return
                    self._open_chat(npc_id, opening=opening)
                    return
            if self._hit_story_button(event.pos):
                self._start_story()
                return
            if self._hit_gear(event.pos):
                self.debug_open = not self.debug_open
                return
            # 怪物画在最上层，点击也优先
            enc = self.renderer.encounter_at(event.pos)
            if enc is not None:
                self._fight_encounter(enc)
                return
            npc_id = self.renderer.npc_at(event.pos)
            if npc_id:
                # 头顶有感叹号：先播支线，没支线才进对话
                if self._start_sidequest(npc_id):
                    return
                self._open_chat(npc_id)
                return
            prop = self.renderer.prop_at(event.pos)
            if prop:
                self._interact(prop)
                return
            # 点空地：走过去（跟着她走的时候不受理，免得一乱点就跑偏）
            if self.following is None:
                wx, wy = self.renderer.to_world(event.pos)
                self._set_target(wx, wy)
                self.pending_enter = None

    # ── 剧情 ──

    def _story_affinity(self, npc_id: str, delta: int, reason: str) -> int:
        """剧情加分走现有的好感引擎，不另开一套数值。"""
        return self.app.deps.affection.apply_node(self.app.profile, npc_id, delta, reason)

    def _npc_crit(self, npc_id: str) -> float:
        """好感 → 她的暴击率。"""
        state = self.app.deps.affection.state_of(self.app.profile, npc_id)
        return crit_for_stage(state.stage.value)

    # ── 决策层要看的玩法快照 ──

    def _game_view(self, npc_id: str) -> dict:
        """把玩法层的数据打包给决策层。"""
        p = self.hub.controller.player
        bag = p.bag
        counts = dict(bag.counts)

        def affordable(station: str) -> list[str]:
            """材料够、现在真做得出来的那些配方。"""
            return [r.id for r in recipes_at(station) if bag.can_afford(r.materials)]

        stage = self.app.deps.affection.state_of(self.app.profile, npc_id).stage
        care = [(s.id, s.label) for s in services.for_stage(npc_id, stage)]

        rank = STAGE_RANK.get(stage, 0)
        invites = ["invite_walk"]
        if rank >= STAGE_RANK[Stage.CLOSE]:
            invites.append("invite_visit")
        if rank >= STAGE_RANK[Stage.AMBIGUOUS]:
            invites.append("request_hug")

        chapter = self.hub.story_chapter
        progress = "自由时间"
        if self.story_view is not None:
            pos = self.story_view.position()
            progress = f"正在播第 {chapter} 章 · {pos.get('label', '')}"

        # 装备挂在身上 ≠ 该修了。只带着不修，决策层会以为她每半小时都想修一次
        wid = bag.equipped.get("weapon") or ""
        need_repair = False
        if wid and wid in ITEMS:
            top = max(1, ITEMS[wid].dur)
            need_repair = bag.durability.get(wid, top) <= top * 0.5

        return {
            "bag": counts,
            # 整张物品表都给，不是只给背包里的
            "item_names": {i: it.name for i, it in ITEMS.items()},
            "food_items": [i for i in counts if i in ITEMS and ITEMS[i].kind == "food"],
            "money": p.money,
            "shop_items": list(SHOP),
            "recipes_kitchen": affordable("kitchen"),
            "recipes_storage": affordable("storage"),
            "equip": [i for i in bag.equipped.values() if i],
            "repair_needed": need_repair,
            "care": care,
            "invites": invites,
            "day": self.hub.clock.day,
            "slot": self.hub.clock.label,
            "player_hp": (p.hp, p.max_hp),
            "player_bag": [ITEMS[i].name for i in counts if i in ITEMS],
            "story_chapter": f"第 {chapter} 章",
            "story_progress": progress,
            "story_busy": self.story_view is not None,
        }

    def _npc_did(self, npc_id: str, payload: dict) -> None:
        """她在行为树里做完一件事，把结果落进玩法层。"""
        act = str(payload.get("act") or "")
        who = self.names.get(npc_id, npc_id)
        ok = False
        msg = ""

        try:
            if act == "cook":
                ok, msg = self.hub.cook(str(payload.get("recipe") or ""))
            elif act == "craft":
                ok, msg = self.hub.craft(str(payload.get("recipe") or ""))
            elif act == "repair":
                ok, msg = self.hub.repair(str(payload.get("mode") or "durability"))
            elif act == "scavenge":
                ok, got = self.hub.npc_gather("rubble")
                msg = f"翻到 {got}" if ok else got
            elif act == "pick_flower":
                ok, got = self.hub.npc_gather("flowers")
                msg = f"摘到 {got}" if ok else got
            elif act == "buy":
                ok, msg = self.hub.buy(str(payload.get("item") or ""))
            elif act == "sell":
                ok, msg = self.hub.sell(str(payload.get("item") or ""))
            elif act == "eat":
                ok, msg = self._npc_eat(npc_id, str(payload.get("item") or ""))
            elif act == "give":
                ok, msg = self._npc_give(npc_id, str(payload.get("item") or ""))
            else:
                return          # 纯表演（loiter / scan / tend …），没有副作用
        except Exception as e:  # noqa: BLE001  她的动作失败不该拖垮主循环
            print(f"[NPC 动作失败] {npc_id} {act}: {e}", flush=True)
            # 崩了也算没成：不回话的话叶子会等到超时再当成功，等于假装没事
            self.app.behavior.set_action_result(npc_id, False, f"出错：{e}")
            return

        # 不管成没成都要回话，叶子在等
        self.app.behavior.set_action_result(npc_id, ok, msg)

        if ok:
            self.hub.save()
            self._toast(f"{who}{msg}", seconds=2.2)
        else:
            # 失败也要让玩家知道——否则她站在灶台前半天没动静，看着像卡住
            print(f"[NPC 动作未成] {npc_id} {act}: {msg}", flush=True)
            # 而且要让她自己知道：记一条"没做成"的流水 + 心情不佳。
            self.app.report_action_failed(npc_id, act, msg)

    def _npc_eat(self, npc_id: str, item_id: str) -> tuple[bool, str]:
        """她吃东西：补她自己的体力，不是给你回血。"""
        bag = self.hub.controller.player.bag
        item = ITEMS.get(item_id)
        if item is None or item.kind != "food" or not bag.has(item_id):
            # 没指定、或已经被吃完了：随便找一样能吃的
            item_id, item = "", None
            for iid, n in bag.counts.items():
                it = ITEMS.get(iid)
                if n > 0 and it is not None and it.kind == "food":
                    item_id, item = iid, it
                    break
        if item is None:
            return False, "没找到能吃的"

        bag.take(item_id, 1)
        state = self.app.deps.npc_states.load(npc_id)
        state.stamina = min(1.0, state.stamina + 0.35)
        self.app.deps.npc_states.save(state)
        return True, f"吃了{item.name}"

    def _npc_give(self, npc_id: str, item_id: str) -> tuple[bool, str]:
        """她把东西递给你。"""
        bag = self.hub.controller.player.bag
        item = ITEMS.get(item_id)
        if item is None or not bag.has(item_id):
            # 没指定 / 那一刻已经没有了：优先递一样吃的（她多半是刚做完的）
            item_id, item = "", None
            for iid, n in bag.counts.items():
                it = ITEMS.get(iid)
                if n > 0 and it is not None and it.kind == "food":
                    item_id, item = iid, it
                    break
        if item is None:
            return False, "手上没东西可给"

        # 主动塞东西是"关心说不出口"的落点，加一点好感
        self.app.deps.affection.apply_node(
            self.app.profile, npc_id, 2, f"给他{item.name}"
        )
        self.app.deps.profiles.save(self.app.profile)
        # 记一笔：下次聊天她记得自己给过什么（source=story 那套写库接口）
        self.app.remember_quest(
            npc_id,
            key=f"给过他_{item_id}",
            value=f"我把{item.name}递到他手上",
            importance=3,
            evidence="（她主动给的）",
        )
        return True, f"把{item.name}递给你"

    def _story_route(self) -> str:
        """第五章之后进哪条线。判定规则见 game_design/06_story.md。"""
        hi = {Stage.AMBIGUOUS, Stage.DEPENDENT}
        a = self.app.deps.affection.state_of(self.app.profile, "A").stage in hi
        b = self.app.deps.affection.state_of(self.app.profile, "B").stage in hi
        if a and b:
            return "harem"
        if a:
            return "lan"
        if b:
            return "mio"
        return "friend"

    def _story_file(self) -> Path:
        no = self.hub.story_chapter
        if no in (6, 8):
            # 用定线时锁住的结果。没锁过（比如 F3 直接跳到第 8 章）才现算
            route = self.hub.story_route or self._story_route()
            return PROJECT_ROOT / "story" / f"ch{no}_{route}.json"
        return PROJECT_ROOT / "story" / f"ch{no}.json"

    def _start_story(self, chapter: int | None = None) -> None:
        if chapter is not None:
            self.hub.story_chapter = chapter
        path = self._story_file()
        if not path.exists():
            self._toast("剧情到这里就没了" if self.hub.story_chapter > 8 else f"找不到 {path.name}")
            return

        loaded = load_chapter(path)

        # 剧情自己决定发生在哪。不这样的话，"第二天一早，岚在闸门口等你"
        if loaded.scene and loaded.scene != self.player_scene:
            if self.app.deps.world.has(loaded.scene):
                self._change_scene(loaded.scene)

        # defer_battle：剧情里的战斗交给外面的节拍（走位 → 开打 → 回来）
        self.story_view = StoryView(
            loaded,
            self.hub.controller,
            fonts=self.fonts,
            defer_battle=True,
            on_day=self.hub.advance_day,
        )
        self._story_file_name = path.name
        self._pending_sidequest = ""
        self._battle_beat = None
        self._sync_story_cursor()
        self._toast(f"第 {self.hub.story_chapter} 章")

    def _resume_story(self) -> None:
        """读档后接着播没播完的那一段。"""
        cur = self.hub.story_cursor
        name = str(cur.get("file", "")) if cur else ""
        if not name:
            return

        path = PROJECT_ROOT / "story" / name
        if not path.exists():
            self.hub.clear_story_cursor()
            return

        view = StoryView(
            load_chapter(path), self.hub.controller, fonts=self.fonts, defer_battle=True, on_day=self.hub.advance_day
        )
        label = str(cur.get("label", ""))
        cursor = int(cur.get("cursor", 0) or 0)
        if not view.restore(label, cursor):
            self.hub.clear_story_cursor()       # 剧本改过，位置失效了，重开这一章
            return

        self.story_view = view
        self._story_file_name = name
        self._pending_sidequest = str(cur.get("sidequest", "") or "")
        self._toast("接着上次的地方", seconds=2.0)

    def _sync_story_cursor(self) -> None:
        """把"播到哪"同步给 Hub。纯内存操作，每帧调也没关系。"""
        view = self.story_view
        if view is None:
            self.hub.clear_story_cursor()
            return
        pos = view.position()
        self.hub.set_story_cursor(
            self._story_file_name,
            str(pos.get("label", "")),
            int(pos.get("cursor", 0) or 0),
            self._pending_sidequest,
        )

    # ── 好感快照 ──

    def _dump_profile(self) -> dict:
        """好感 + 剧情标记。"""
        aff = {}
        for nid, st in self.app.profile.affection.items():
            aff[nid] = {
                "affinity": st.affinity,
                "stage": st.stage.value,
                "daily_gain": st.daily_gain,
                "last_gain": st.last_gain.isoformat() if st.last_gain else None,
            }
        return {"affection": aff, "flags": dict(self.app.profile.flags)}

    def _load_profile(self, data: dict) -> None:
        for nid, raw in (data.get("affection") or {}).items():
            st = self.app.profile.affection.get(nid)
            if st is None:
                st = AffectionState()
                self.app.profile.affection[nid] = st
            st.affinity = int(raw.get("affinity", st.affinity))
            try:
                st.stage = Stage(str(raw.get("stage", st.stage.value)))
            except ValueError:
                pass                      # 存档里的阶段名不认识就保持原样，不炸
            st.daily_gain = int(raw.get("daily_gain", st.daily_gain))
            last = raw.get("last_gain")
            st.last_gain = date.fromisoformat(last) if last else None
        self.app.profile.flags.update(data.get("flags") or {})

    # ── 读档 ──

    def _load_game(self) -> None:
        """读档：玩法状态 + 好感 + 剧情位置 + NPC 站位，一次全拉回来。"""
        if not self.hub.load():
            self._toast("还没有存档")
            return

        # 行为层：中断正在跑的行为树，把人放回存档里的场景
        self.app.behavior.interrupt_all("load")
        for nid, rt in self.hub.controller.npcs.items():
            if rt.place:
                self.app.behavior.spawn(nid, rt.place)
        # 再把"她本来做到哪一步"接回来。顺序不能反
        restored = self.app.behavior.plans_restore(self.hub.loaded_plans or {})
        if restored:
            self._toast(f"接着上次做（{restored} 人）", seconds=2.0)

        # 三个全屏界面都清掉，从干净状态重开
        self.story_view = None
        self.battle_view = None
        self.panel_view = None
        self._battle_beat = None
        self.pending_say.clear()
        self.pending_offer.clear()
        self._chat_busy = False
        if self.panel.is_open:
            self.panel.close()
        self._pending_sidequest = ""
        self._story_file_name = ""

        self._change_scene(self.hub.controller.player.place or "outdoor")
        self._resume_story()          # 存档里有没播完的剧情就接着播
        self._toast("读档完成", seconds=2.0)

    def _reset_progress(self) -> None:
        """重置：剧情回第一章，好感与记忆清零。"""
        now = pygame.time.get_ticks() / 1000
        if now - self._reset_armed_at > 4.0:
            self._reset_armed_at = now
            self._toast(
                "再按一次 F8 确认重置（剧情回第一章 · 好感与记忆清零）", seconds=4.0
            )
            return
        self._reset_armed_at = 0.0

        # 界面先回到干净状态，和读档同一套清理。
        self.story_view = None
        self.battle_view = None
        self.panel_view = None
        self._battle_beat = None
        self.pending_say.clear()
        self.pending_offer.clear()
        self._chat_busy = False
        if self.panel.is_open:
            self.panel.close()
        self._pending_sidequest = ""
        self._story_file_name = ""

        # 1. app 层：好感归零、记忆清空并落盘、决策状态归零。
        self.app.reset_progress()
        # 2. 旧决策全部作废：重置前提交的那批是拿旧好感算的
        self.app.deps.decisions.discard()
        self.app.deps.decisions.resume()

        # 3. 玩法层：剧情回第一章，任务重来，剧情位置清掉
        self.hub.reset_progress(1)

        # 4. 先归零再写档。顺序反了会把旧的好感/章节写回文件
        self.hub.save()

        self._change_scene("outdoor")
        self._toast("已重置：剧情回到第一章，好感与记忆清零", seconds=3.0)

    def _finish_story(self) -> None:
        self.story_view = None
        self._battle_beat = None
        self._close_panel()
        self.hub.clear_story_cursor()
        self._story_file_name = ""

        qid, self._pending_sidequest = self._pending_sidequest, ""
        if qid:
            q = self.hub.finish_quest(qid)
            self.hub.save()
            if q is not None:
                self._toast(f"支线完成 · {q.name}　好感 +{q.affinity}", seconds=3.2)
            else:
                self._toast("支线结束")
            return

        self.hub.story_chapter += 1
        # 不锁的话第 8 章会按当时的好感重新算，可能跟第 6 章不是同一条线。
        if self.hub.story_chapter >= 6 and not self.hub.story_route:
            self.hub.story_route = self._story_route()
        unlocked = self.hub.sync_chapter(self.hub.story_chapter)
        self.hub.save()
        tip = f"第 {self.hub.story_chapter - 1} 章结束"
        if self.hub.story_chapter == 6:
            names = {"lan": "岚线", "mio": "澪线", "harem": "后宫线", "friend": "队友线"}
            label = names.get(self.hub.story_route)
            if label:
                tip += f" · 进{label}"
        if unlocked:
            tip += " · 解锁支线 " + "、".join(unlocked)
        self._toast(tip, seconds=3.2)

    # ── 支线 ──

    def _sidequest_file(self, qid: str) -> Path:
        return PROJECT_ROOT / "story" / f"side_{qid}.json"

    def _start_sidequest(self, npc_id: str) -> bool:
        """她有支线就播，返回是否播了。没支线就返回 False，外面照常开对话。"""
        avail = self.hub.sidequests_for(npc_id)
        if not avail:
            # 今天已经和她做过一件、后面还有没做的：说一声，别让人以为功能坏了
            if self.hub.sidequest_locked_today(npc_id):
                self._toast("今天已经陪她做过一件事了，明天再来")
            return False

        q = avail[0]
        path = self._sidequest_file(q.id)
        if not path.exists():
            self._toast(f"缺少剧情脚本 side_{q.id}.json")
            return False
        if not self.hub.spend():
            self._toast("今天的行动点用完了，睡一觉再说")
            return False

        self.hub.start_quest(q.id)
        self._pending_sidequest = q.id
        self._story_file_name = path.name

        loaded = load_chapter(path)
        # 支线也自己声明发生在哪："岚在闸门边上站着"就得把她放到闸门口
        if loaded.scene and loaded.scene != self.player_scene:
            if self.app.deps.world.has(loaded.scene):
                self._change_scene(loaded.scene)

        self._battle_beat = None
        self.story_view = StoryView(
            loaded,
            self.hub.controller,
            fonts=self.fonts,
            defer_battle=True,
            on_day=self.hub.advance_day,
        )
        self._sync_story_cursor()
        self._toast(f"支线 · {q.name}")
        return True

    # ── 剧情战斗节拍 ──

    def _begin_battle_beat(self, payload: dict) -> None:
        """剧情要求打一场：先把人带到地方，再开打。"""
        enemies = [e for e in (payload.get("enemies") or []) if e in MONSTERS]
        if not enemies:
            # 剧本写了 battle 却没写敌人：跳过，别让剧情卡在这一步
            if self.story_view is not None:
                self.story_view.resolve_battle()
            return

        world = self.app.deps.world
        scene = str(payload.get("scene") or self.player_scene)
        if not world.has(scene):
            scene = self.player_scene

        if scene != self.player_scene:
            self._change_scene(scene)

        spot = _battle_spot(world, scene, str(payload.get("zone") or ""))
        self.pending_enter = None          # 别让残留的"进门"指令把人再切走
        self._set_target(float(spot[0]), float(spot[1] + 110))

        solo = str(payload.get("solo") or "")
        # 同伴跟她一起到：不然"她带你来的"这件事在画面上不成立
        companions = [solo] if solo else ["A", "B"]
        for i, nid in enumerate(companions):
            self.app.behavior.spawn(
                nid, scene, xy=(float(spot[0] - 90 + i * 130), float(spot[1] + 150))
            )

        self._battle_beat = {
            "scene": scene,
            "spot": spot,
            "enemies": enemies,
            "solo": solo,
            "phase": "move",
            "t": 0.0,
        }

    def _tick_battle_beat(self, dt: float) -> None:
        beat = self._battle_beat
        if beat is None:
            return
        beat["t"] += dt

        if beat["phase"] == "move":
            self._update_player(dt)
            # 走到了就开打；卡住超过 9 秒（目标被墙夹住、路上被挡）也开打
            if self.player_target is None or beat["t"] > 9.0:
                self.player_target = None
                beat["phase"] = "fight"
                beat["t"] = 0.0
                self._battle(list(beat["enemies"]), solo=beat["solo"], from_story=True)
            return

        # fight：演出关掉就算打完
        if self.battle_view is not None:
            self.battle_view.update(dt)
            return

        self._battle_beat = None
        # 剧情可能已经推进到下一场战斗（剧本里连着两场）
        if self.story_view is not None:
            ev = self.story_view.take_pending_battle()
            if ev is not None:
                self._begin_battle_beat(ev.payload)

    def _battle(
        self,
        enemy_ids: list[str],
        *,
        solo: str = "",
        label: str = "",
        from_story: bool = False,
    ) -> None:
        """打一场。结算立刻完成，演出交给 BattleView 逐条播。"""
        if not enemy_ids:
            return
        party = (solo,) if solo else None
        result = self.hub.controller.fight(enemy_ids, names=self.names, party_npcs=party)
        if not label:
            label = "、".join(MONSTERS[e].name for e in enemy_ids if e in MONSTERS)

        # 关键顺序：结算已经做完了，先把剧情推进到战斗之后，再开演出。
        if from_story and self.story_view is not None:
            self.story_view.resolve_battle()
            self._sync_story_cursor()

        self.battle_view = BattleView(result, label or "战斗", fonts=self.fonts)
        self.hub.save()

    def _fight_encounter(self, enc) -> None:
        if not self.hub.spend():
            self._toast("今天的行动点用完了")
            return
        result = self.hub.controller.fight([enc.monster_id], names=self.names)
        if result.won:
            self.hub.encounters.remove(enc.key)
        self.hub.save()          # 输了也要存：血已经掉了，别退出就白掉
        self.battle_view = BattleView(result, enc.name, fonts=self.fonts)

    def _close_battle(self) -> None:
        """只负责关界面。剧情推进已经在 `_battle` 里做掉了。"""
        self.battle_view = None

    # ── 面板 ──

    def _open_panel(self, kind: str, station: str = "kitchen") -> None:
        self.panel_view = make_panel(kind, self.hub, fonts=self.fonts, station=station)

    def _close_panel(self) -> None:
        self.panel_view = None

    def _set_target(self, x: float, y: float) -> None:
        """设定玩家目标点，并夹到当前场景的可行走范围内。"""
        left, top, right, bottom = self.app.deps.world.bounds(self.player_scene)
        self.player_target = (
            max(left, min(right, x)),
            max(top, min(bottom, y)),
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
        if prop.interact == "shop":
            self._open_panel("shop")
            return
        if prop.interact == "craft":
            station = CRAFT_STATION.get(self.player_scene, "storage")
            self._open_panel("craft", station)
            return
        if prop.interact == "gather":
            ok, msg = self.hub.gather(prop.id)
            self._toast(msg, seconds=2.2)
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
        # 相机直接跳到位。不 snap 的话切场景第一帧会从上一个场景的位置滑过来
        self.renderer.snap(
            world.scene(scene_id), (self.player_xy[0], self.player_xy[1])
        )

        # 玩法层同步：位置存档 + 进场景重新刷怪
        self.hub.controller.player.place = scene_id
        self.hub.enter_scene(scene_id)
        self.hub.save()

        self._toast(world.scene(scene_id).name, seconds=1.2)

    # ── 会话 ──

    def _open_chat(self, npc_id: str, opening: str | None = None) -> None:
        if self._chat_busy:
            return

        # 玩家可能点的是"想和你说话"卡片，也可能直接点人。
        if opening is None:
            opening = self.pending_say.pop(npc_id, ("", 0.0))[0] or None
        else:
            self.pending_say.pop(npc_id, None)

        # opening 会作为第 0 轮写进 transcript，她的回答才接得上自己刚说的话
        self.app.enter_chat(npc_id, opening=opening)
        # 软中断：她手上的事冻住（树、走位、煮到一半的汤都留着）
        self.app.behavior.pause(npc_id, "chat")
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
        self._close_deadline = time.monotonic() + 120.0
        talking = self.panel.npc_id
        self.panel.close()
        # 对话结束 = 软中断结束。她刚才做到哪儿，就从哪儿接着做。
        self.app.behavior.resume(talking)
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
        """对话下方的按钮：只在她说出口之后才出现。"""
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
        """她主动开口时，弹一张可点的卡片。"""
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

        # 剧情战斗节拍：走位 / 开打。它比 battle_view 更外层
        if self._battle_beat is not None:
            self._tick_battle_beat(dt)
            return

        # 战斗演出和弹窗面板都独占画面，底下什么都不推进
        if self.battle_view is not None:
            self.battle_view.update(dt)
            return

        if self.panel_view is not None:
            self.panel_view.update(dt)
            return

        if self.story_view is not None:
            self.story_view.update(dt)
            self._sync_story_cursor()
            ev = self.story_view.take_pending_battle()
            if ev is not None:
                self._begin_battle_beat(ev.payload)
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

            # 她在行为树里做完了一件事（做饭、翻废料、买、修……）。
            if event.kind == "npc_action":
                self._npc_did(event.npc_id, event.payload)
                continue

            # 她明确提出了某件事（"我给你个东西""要不要一起走走"）。
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
        self._clamp_player()
        # 直接写行为层的玩家状态，不走 move_player（那会每帧发一次事件）
        self.app.behavior.player_place = self.player_scene
        self.app.behavior.player_xy = (self.player_xy[0], self.player_xy[1])

    def _clamp_player(self) -> None:
        """夹到当前场景的可行走矩形里。"""
        left, top, right, bottom = self.app.deps.world.bounds(self.player_scene)
        self.player_xy[0] = max(left, min(right, self.player_xy[0]))
        self.player_xy[1] = max(top, min(bottom, self.player_xy[1]))

    def _advance_time(self) -> None:
        """睡一觉：直接到第二天。"""
        day = self.hub.advance_day()
        self.hub.save()
        self._toast(f"第 {day} 天 · 行动点回满了", seconds=2.2)
        self._idle_timer = 0.0

    # ── 绘制 ──

    def _aim_camera(self) -> None:
        """把相机对准玩家。每帧一次，平滑靠 renderer 内部插值。"""
        self.renderer.follow(
            self.app.deps.world.scene(self.player_scene),
            (self.player_xy[0], self.player_xy[1]),
        )

    def _draw(self) -> None:
        if self.loading:
            self._draw_loading()
            return

        self._aim_camera()

        # 对话面板是全屏的，不用画场景
        if self.panel.is_open:
            self.panel.draw(self.screen)
            self._draw_toast()
            return

        # 场景里的怪 + 剧情战斗的目标怪（走位阶段要看得见要打谁）
        encounters = list(
            self.hub.encounters.items if self.hub.encounters.scene_id == self.player_scene else ()
        )
        beat = self._battle_beat
        if beat is not None and beat["phase"] == "move" and beat["scene"] == self.player_scene:
            sx, sy = beat["spot"]
            encounters.append(
                Encounter(
                    key="beat:target",
                    monster_id=beat["enemies"][0],
                    area="beat",
                    x=int(sx) - 32,
                    y=int(sy) - 32,
                )
            )

        markers = frozenset(nid for nid in ("A", "B") if self.hub.has_marker(nid))
        self.renderer.draw(
            self.screen,
            scene_id=self.player_scene,
            agents=self.app.behavior.agents(),
            player_xy=(self.player_xy[0], self.player_xy[1]),
            player_name=self.app.profile.name,
            encounters=encounters,
            markers=markers,
        )

        if self.battle_view is not None:
            self.battle_view.draw(self.screen)
            return

        # 走位阶段不画剧情对话框——玩家得看见自己在往哪儿走。
        if self.story_view is not None and beat is None:
            self.story_view.draw(self.screen)
            self._draw_toast()
            return

        if self.panel_view is not None:
            self.panel_view.draw(self.screen)
            return

        self._draw_topbar()
        if beat is not None:
            self._draw_beat_banner(beat)
        self._draw_offscreen_markers()
        self._draw_hint()
        self._draw_keys()
        self._draw_pending_say()
        self._draw_gear()
        self._draw_toast()
        if self.debug_open:
            self._draw_debug()

    def _draw_beat_banner(self, beat: dict) -> None:
        """走位时的横幅：告诉玩家现在不是卡住了，是在往战场走。"""
        names = "、".join(MONSTERS[e].name for e in beat["enemies"] if e in MONSTERS)
        if beat["phase"] == "move":
            text = f"跟过去 · 前面有{names}"
        else:
            text = f"对上了 · {names}"

        font = self.fonts.get(17, bold=True)
        label = font.render(text, True, T.ACCENT)
        box = pygame.Rect(
            SCREEN_W // 2 - label.get_width() // 2 - 20, 54, label.get_width() + 40, 34
        )
        bg = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.rect(bg, (14, 16, 20, 210), bg.get_rect(), border_radius=17)
        pygame.draw.rect(bg, (*T.ACCENT_DIM, 255), bg.get_rect(), 1, border_radius=17)
        self.screen.blit(bg, box.topleft)
        self.screen.blit(label, (box.left + 20, box.top + 6))

    def _draw_offscreen_markers(self) -> None:
        """她在这个场景、但不在画面里时，屏幕边缘给一个指向。"""
        for npc_id in ("A", "B"):
            agent = self.app.behavior.agent(npc_id)
            if agent.place != self.player_scene:
                continue
            sx, sy = self.renderer.to_screen((agent.x, agent.y))
            if -40 <= sx <= SCREEN_W + 40 and 0 <= sy <= SCREEN_H:
                continue      # 已经在画面里，不需要提示

            # 贴到屏幕边上：留出顶部状态栏和底部按键提示的空间
            cx = max(80, min(SCREEN_W - 80, sx))
            cy = max(70, min(SCREEN_H - 60, sy))
            color = T.NPC_A_BODY if npc_id == "A" else T.NPC_B_BODY

            pygame.draw.circle(self.screen, (14, 16, 20), (int(cx), int(cy)), 15)
            pygame.draw.circle(self.screen, color, (int(cx), int(cy)), 15, 2)
            # 箭头指向真实方向
            dx, dy = sx - cx, sy - cy
            length = max(1.0, (dx * dx + dy * dy) ** 0.5)
            tip = (cx + dx / length * 22, cy + dy / length * 22)
            pygame.draw.circle(self.screen, color, (int(tip[0]), int(tip[1])), 4)

            label = self.fonts.get(13, bold=True).render(
                self.names.get(npc_id, npc_id), True, color
            )
            self.screen.blit(
                label, (int(cx - label.get_width() / 2), int(cy + 18))
            )

    def _draw_keys(self) -> None:
        """常驻的按键提示。放左下角，不挡视线。"""
        f = self.fonts.get(13)
        img = f.render(
            "点左上「第N章」播剧情 · B 背包 · Q 任务 · F5 存 / F9 读 · F8 重置（双按） · F1 调试 · F2 睡一觉",
            True,
            T.TEXT_FAINT,
        )
        self.screen.blit(img, (16, SCREEN_H - 26))

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

        # 场景名右边就是主线入口。这是玩家找剧情最自然的地方——
        self._draw_story_button(24 + name.get_width() + 18)

        # 中间：游戏时间 + 行动点。行动点用完了要一眼能看见
        clock = self.hub.clock
        p = self.hub.controller.player
        left = f"第 {clock.day} 天 · {clock.label} · 行动 {clock.points}/{SLOTS_PER_DAY}"
        color = T.DANGER if clock.points <= 0 else T.TEXT_DIM
        img = self.fonts.get(15).render(left, True, color)
        self.screen.blit(img, (SCREEN_W // 2 - img.get_width() // 2, 13))

        right = f"血 {p.hp}/{p.max_hp}　票据 {p.money}　背包 {sum(p.bag.counts.values())}"
        if self.player_scene != "outdoor":
            right += "　ESC 返回"
        info = self.fonts.get(15).render(right, True, T.TEXT_DIM)
        self.screen.blit(info, (SCREEN_W - info.get_width() - 24, 13))

    def _draw_story_button(self, x: int) -> None:
        """主线剧情按钮：显示当前章节，点一下就开始播。"""
        ch = self.hub.story_chapter
        over = ch > 8
        if over:
            text, color = "剧情已看完", T.TEXT_FAINT
        else:
            text, color = f"▶ 第 {ch} 章", T.ACCENT

        font = self.fonts.get(15, bold=True)
        label = font.render(text, True, color)
        box = pygame.Rect(x, 7, label.get_width() + 26, 26)
        self._story_btn = box

        hover = box.collidepoint(pygame.mouse.get_pos()) and not over
        if hover:
            # screen 不是 SRCALPHA surface，直接画 4 元组颜色会忽略 alpha
            bg = pygame.Surface(box.size, pygame.SRCALPHA)
            pygame.draw.rect(bg, (*T.ACCENT_DIM, 70), bg.get_rect(), border_radius=13)
            self.screen.blit(bg, box.topleft)
        pygame.draw.rect(
            self.screen, color if hover else T.PANEL_LINE, box, 1, border_radius=13
        )
        self.screen.blit(label, (box.left + 13, box.top + 4))

    def _hit_story_button(self, pos) -> bool:
        btn = getattr(self, "_story_btn", None)
        return bool(btn is not None and btn.collidepoint(pos))

    def _draw_hint(self) -> None:
        near = self.renderer.npc_at(pygame.mouse.get_pos())
        if not near:
            return
        who = self.names.get(near, near)
        if self.hub.has_marker(near):
            text = f"{who}好像有事找你"
        else:
            text = f"点击和{who}说话"
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
        # 从 600 起会让卡片压到 toast（toast 在 SCREEN_H-120=690 居中）。
        y = SCREEN_H - 250

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
        # 放在 770 而不是 758：右下角"点击和谁说话"那个提示条占 720~760
        box = pygame.Rect(1398, 770, 30, 30)
        gear = pygame.Surface(box.size, pygame.SRCALPHA)
        pygame.draw.circle(gear, (220, 220, 220, 50), (15, 15), 9)
        pygame.draw.circle(gear, (220, 220, 220, 20), (15, 15), 13, 2)
        self.screen.blit(gear, box.topleft)

    def _hit_gear(self, pos) -> bool:
        return pygame.Rect(1398, 770, 30, 30).collidepoint(pos)

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
            f"第 {self.hub.clock.day} 天 {self.hub.clock.label}"
            f" · 行动 {self.hub.clock.points}/{SLOTS_PER_DAY}"
            f" · 票据 {self.hub.controller.player.money}",
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
        pst = self.app.deps.planner.stats()
        lines.append(
            f"决策队列 {q_state}  待{int(q['pending'])}  批{int(q['runs'])}/{int(q['last_batch'])}"
            f"  失败{int(pst['failures'])}"
        )
        # A/B 的时候最需要看这一行：这轮目标是谁定的、模型选了什么、给了什么理由
        if pst.get("intent_mode") == "llm":
            lines.append(f"意图来源 LLM 自选（F4 切回规则）")
            lines.append(f"意图/LLM  {pst.get('intent_raw') or '（这轮没给，已退回规则）'}")
        else:
            lines.append("意图来源 规则打分（F4 切到 LLM）")
        lines.append("F5 存档 · F8 重置（剧情/好感/记忆）· F9 读档")

        err = self.app.deps.stylist.last_error
        if err:
            lines.append(f"上次聊天失败：{err}")

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
