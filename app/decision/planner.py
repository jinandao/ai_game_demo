"""三段式规划：定目标、排动作、翻工具链。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from ..action import services
from ..domain.enums import DecisionIntent, Stage
from ..domain.models import Decision, NpcEvent, NpcState, Persona, Profile
from ..infra.clock import Clock
from ..infra.config import Config
from ..infra.llm import LLMClient, LLMError
from ..memory.retriever import MemoryRetriever
from ..prompts.decision import build_text_plan_system, build_tool_system
from . import actions as action_lib
from . import goals as goal_lib
from .intent_llm import MODES as INTENT_MODES
from .intent_llm import LLMIntentPicker
from .plan import (
    PlanResult,
    describe,
    describe_steps,
    parse_cn_plan,
    parse_plan,
    read_cn_steps,
    render_cn,
)
from .rules import RuleEngine
from .view import WorldView

MAX_PARALLEL = 4

# ②现在是结构化的中文动作，每条要带「动作」+参数。
TEXT_PLAN_MAX_TOKENS = 480

# 最多 8 条动作，每条带参数，700 留余量。
TOOL_PLAN_MAX_TOKENS = 700

STAGE_CN = {
    "stranger": "陌生",
    "acquaintance": "熟人",
    "close": "亲近",
    "ambiguous": "暧昧",
    "dependent": "依赖",
}

def _t() -> str:
    """日志用的墙钟时间。"""
    return time.strftime("%H:%M:%S")

def _mood_cn(value: object) -> str:
    """mood 是 0~1 的浮点，直接丢给模型它只会看到「0.5」。"""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "平静"
    if x < 0.3:
        return "很低落"
    if x < 0.5:
        return "有点闷"
    if x < 0.7:
        return "平静"
    return "心情不错"

def _stamina_cn(value: float) -> str:
    if value < 0.2:
        return "快撑不住了"
    if value < 0.4:
        return "有点累"
    if value < 0.7:
        return "还行"
    return "精神很好"

# 失败动作 → 它属于哪个目标。`core.report_action_failed` 写的是动作 id
_FAIL_GOAL = {
    "buy": "shop_buy",
    "sell": "shop_sell",
    "cook": "cook",
    "craft": "craft",
    "repair": "repair",
    "scavenge": "gather",
    "pick_flower": "pick_flower",
    "eat": "eat",
    "give": "serve",
}

class DecisionPlanner:
    def __init__(
        self,
        llm: LLMClient,
        rules: RuleEngine,
        retriever: MemoryRetriever,
        clock: Clock,
        personas: dict[str, Persona] | None = None,
        max_parallel: int = MAX_PARALLEL,
        intent_mode: str = "rule",
        cfg: Config | None = None,
    ) -> None:
        self._llm = llm
        self._rules = rules
        self._retriever = retriever
        self._clock = clock
        self._personas = personas or {}
        self._max_parallel = max(1, max_parallel)
        # 只用来取"填几条记忆"这类配置；不传就用默认值，测试不用构造 Config
        self._cfg = cfg or Config()
        # ① 用规则还是用模型。两种模式只换选择器，②③完全一样
        self._intent_mode = intent_mode if intent_mode in INTENT_MODES else "rule"
        self._intent_llm = LLMIntentPicker(llm) if self._intent_mode == "llm" else None
        self._failures = 0
        self._last_error = ""
        # 调试面板用的三个摘要
        self._last_goal = ""
        self._last_text_plan = ""
        self._last_plan = ""
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_parallel, thread_name_prefix="decide-npc"
        )

    # ── 对外 ──

    def set_intent_mode(self, mode: str) -> str:
        """运行中切换①的来源。"""
        if mode not in INTENT_MODES:
            return self._intent_mode
        if mode != self._intent_mode:
            self._intent_mode = mode
            self._intent_llm = LLMIntentPicker(self._llm) if mode == "llm" else None
            print(
                f"[决策] 意图来源 → {'LLM 自选（对比实验）' if mode == 'llm' else '规则打分'}",
                flush=True,
            )
        return self._intent_mode

    def toggle_intent_mode(self) -> str:
        return self.set_intent_mode(
            "rule" if self._intent_mode == "llm" else "llm"
        )

    def decide(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        *,
        here: str = "",
        player_at: str = "",
        view: WorldView | None = None,
        avoid: str = "",
    ) -> NpcEvent:
        view = view or WorldView(here=here, player_at=player_at)
        stage = self._stage_of(profile, npc_id)

        # ── ① 定目标：规则打分，或者模型自己选 ──
        pick, recent_items = self._intent_of(
            npc_id, profile, state, view, avoid=avoid
        )
        mark = "LLM" if self._intent_mode == "llm" else "规则"
        print(f"[{_t()}] [目标/{mark}] {npc_id} {pick.text} · {pick.because}", flush=True)

        # ── ② 模型：在"她这一刻真能做的动作"里排一串 ──
        options = self._options(npc_id, stage, view)
        pairs = self._value_pairs(npc_id, stage, view)
        steps = self._ask_text_plan(
            npc_id, pick, profile, state, view, recent_items, pairs
        )

        if steps:
            plan_text = render_cn(steps)
            print(f"[{_t()}] [打算] {npc_id} {' → '.join(plan_text)}", flush=True)
        else:
            # ②整个没给出来：用目标自带的兜底步骤（人话），③照样能翻
            plan_text = list(pick.goal.steps)
            print(f"[{_t()}] [打算] {npc_id} 退回兜底：{' → '.join(plan_text)}", flush=True)

        # ── ③ 模型：中文动作 → 工具串 ──
        result = self._ask_tools(npc_id, pick, steps or plan_text, profile, view, options)

        # ③ 自己挂了（模型超时 / 输出全废）时别让她站着
        if (result is None or not result.ok) and steps:
            rescue = parse_cn_plan(
                {"plan": steps}, goal=pick.goal, values=pairs, room=view.room
            )
            if rescue.ok:
                print(f"[工具] {npc_id} ③没翻出来，按中文动作逐条兜底", flush=True)
                result = rescue

        # ③ 真正要执行的那一串，单独打一行。
        if result is not None and result.steps:
            print(f"[{_t()}] [工具链] {npc_id} {describe_steps(result.steps)}", flush=True)

        self._last_goal = pick.text
        self._last_text_plan = " → ".join(plan_text)
        self._last_plan = describe(pick.goal, result.steps if result else [])
        if result and result.dropped:
            print(f"[工具] {npc_id} 丢掉 {len(result.dropped)} 条：{result.dropped}", flush=True)

        return NpcEvent(
            npc_id=npc_id,
            decision=Decision(
                intent=pick.intent,
                # 目标为什么被选中——规则的依据，不是模型编的理由
                reason=pick.because,
                urgency=min(0.95, max(0.2, pick.urge)),
                goal_text=pick.text,
                plan_text=tuple(plan_text),
                script=result.script if result else None,
            ),
        )

    def _intent_of(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        view: WorldView,
        *,
        avoid: str = "",
        use_llm: bool = True,
    ) -> tuple[goal_lib.Pick, list]:
        """① 定目标：纯打分，不产出任何动作、不动世界。"""
        stage = self._stage_of(profile, npc_id)
        allowed, recent_items = self._collect(npc_id, profile, state)

        if avoid:
            stripped = {i for i in allowed if i.value != avoid}
            if stripped:            # 别把候选剪空，剪空就退回原来的集合
                allowed = stripped

        situation = goal_lib.Situation.of(
            view,
            state,
            stage,
            npc_id=npc_id,
            # 性格权重进公式：同一个局面，岚和澪算出来的第一名本来就该不一样
            bias=self._personas[npc_id].bias if npc_id in self._personas else (),
            last_goal=self._prev_goal(state, recent_items),
            # 刚失败的事：规则层给它降权，别"马上再来一次"
            last_failed=self._prev_failure(recent_items),
        )

        if use_llm:
            pick = self._choose_intent(
                npc_id, profile, state, view, recent_items, situation, allowed
            )
            # 模型自己选的（LLM 模式）可能不理会 allowed，这里兜一下
            if avoid and pick.goal.id == avoid:
                pick = goal_lib.pick(situation, allowed)
        else:
            pick = goal_lib.pick(situation, allowed)
        return pick, recent_items

    def _split_intents(
        self, requests: dict[str, tuple[Profile, NpcState, str, str, WorldView]]
    ) -> dict[str, str]:
        """一批里谁先占住哪个独占目标。"""
        taken: dict[str, str] = {}       # 目标 id → 占住它的 npc
        avoid: dict[str, str] = {}
        for npc_id, (profile, state, _here, _player_at, view) in requests.items():
            pick, _recent = self._intent_of(
                npc_id, profile, state, view, use_llm=False
            )
            gid = pick.goal.id
            if gid in taken and gid in goal_lib.EXCLUSIVE:
                avoid[npc_id] = gid
                print(
                    f"[决策] {npc_id} 和 {taken[gid]} 都想「{pick.goal.label}」，"
                    f"她换一件",
                    flush=True,
                )
                continue
            taken.setdefault(gid, npc_id)
        return avoid

    def decide_many(
        self, requests: dict[str, tuple[Profile, NpcState, str, str, WorldView]]
    ) -> dict[str, NpcEvent]:
        """多个 NPC 同时决策，每人一套完整提示词。"""
        if not requests:
            return {}
        if len(requests) == 1:
            npc_id, (profile, state, here, player_at, view) = next(iter(requests.items()))
            return {
                npc_id: self.decide(
                    npc_id, profile, state, here=here, player_at=player_at, view=view
                )
            }

        # 先串行把目标定下来、把撞车的拆开（纯规则，微秒级），再并发跑 ②③
        avoid = self._split_intents(requests)

        futures = {}
        for npc_id, (profile, state, here, player_at, view) in requests.items():
            futures[
                self._pool.submit(
                    self.decide,
                    npc_id,
                    profile,
                    state,
                    here=here,
                    player_at=player_at,
                    view=view,
                    avoid=avoid.get(npc_id, ""),
                )
            ] = npc_id

        out: dict[str, NpcEvent] = {}
        for future, npc_id in futures.items():
            try:
                out[npc_id] = future.result()
            except Exception:  # noqa: BLE001  单个失败不能拖垮整批
                self._failures += 1
                profile, state, _here, _player_at, _view = requests[npc_id]
                allowed, _ = self._collect(npc_id, profile, state)
                out[npc_id] = NpcEvent(
                    npc_id=npc_id, decision=self._fallback(allowed, "decide_failed")
                )
        return out

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def stats(self) -> dict[str, object]:
        return {
            "failures": self._failures,
            "error": self._last_error,
            "goal": self._last_goal,
            "text_plan": self._last_text_plan,
            "plan": self._last_plan,
            # 规则 / llm：调试面板上要能一眼看出这轮是谁定的目标
            "intent_mode": self._intent_mode,
            "intent_raw": self._intent_llm.stats()["raw"] if self._intent_llm else "",
        }

    def _choose_intent(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        view: WorldView,
        recent_items: list,
        situation: goal_lib.Situation,
        allowed: set[DecisionIntent],
    ) -> goal_lib.Pick:
        """①：规则打分，或者模型自己选。"""
        if self._intent_llm is not None:
            payload = self._situation(npc_id, profile, state, view, recent_items)
            payload.update(self._abilities(view))
            got = self._intent_llm.pick(payload, allowed, situation)
            if got is not None:
                return got
            print(f"[目标/LLM] {npc_id} 这一轮没选出来，退回规则", flush=True)
        return goal_lib.pick(situation, allowed)

    # ── 上下文 ──

    def _collect(
        self, npc_id: str, profile: Profile, state: NpcState
    ) -> tuple[set[DecisionIntent], list]:
        allowed = self._rules.allowed(
            profile=profile, npc_id=npc_id, state=state, now=self._clock.now()
        )
        # 动作流水（最近做过什么，用来避免"刚睡完又去睡"）
        recent_items = self._retriever.events(npc_id, self._cfg.decision_event_k)

        # 这个阶段一件照料动作都做不了，就不该让她想"照顾他"
        stage = self._stage_of(profile, npc_id)
        if not services.for_stage(npc_id, stage):
            allowed = allowed - {DecisionIntent.SERVE}
        return allowed, recent_items

    @staticmethod
    def _stage_of(profile: Profile, npc_id: str) -> Stage:
        affection = profile.affection.get(npc_id)
        return affection.stage if affection else Stage.STRANGER

    @staticmethod
    def _prev_goal(state: NpcState, recent_items: list) -> str:
        """上一轮目标 id，用来给"刚做过"的目标降权。"""
        last = state.last_decision
        if last is not None:
            return last.intent.value
        for m in recent_items:
            if m.source == "decision" and m.key.startswith("决策_"):
                return m.key[len("决策_"):]
        return ""

    @staticmethod
    def _prev_failure(recent_items: list) -> str:
        """最近一条"没做成"的流水 → 它对应的目标 id（没有就空）。"""
        for m in recent_items:          # 新的在前
            key = str(getattr(m, "key", "") or "")
            if key.startswith("失败_"):
                return _FAIL_GOAL.get(key[len("失败_"):], "")
        return ""

    def _situation(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        view: WorldView,
        recent_items: list,
    ) -> dict:
        """两段提示词共用的处境描述。"""
        persona = self._personas.get(npc_id)
        stage = self._stage_of(profile, npc_id)
        unresolved = self._retriever.unresolved(profile.player_id, npc_id)

        # 最近的动作流水（已经按档取好了，就是传进来的 recent_items）
        decided = [
            (m.value or "").strip()
            for m in recent_items
            if not str(getattr(m, "key", "")).startswith("失败_")
        ]
        failed = [
            (m.value or "").strip()
            for m in recent_items
            if str(getattr(m, "key", "")).startswith("失败_")
        ][:2]
        # 深刻记忆：剧情里发生过的 + 她为他做过的事，各取最近几条
        deep_items = self._retriever.deep(npc_id, self._cfg.decision_deep_k)
        remembered = [(m.value or "").strip() for m in deep_items]
        # 玩家画像：对话里抽出来的「他是谁、他喜欢什么、他想要什么」
        portrait = [
            (m.value or "").strip()
            for m in self._retriever.interactions(npc_id, self._cfg.decision_deep_k)
        ]

        around: dict[str, str] = {}
        for sid, amb in view.scene_ambience.items():
            name = view.scene_names.get(sid, sid)
            props = view.scene_props.get(sid, "")
            around[name] = f"{amb} {props}".strip() if props else amb

        # 物品要用中文名：给模型看 "废金属×5"，不是 "scrap×5"。
        carried = [
            f"{view.item_names.get(item, item)}×{n}"
            for item, n in sorted(view.bag.items())
            if n > 0
        ]
        # 走得到的地方。不给这张表，模型只从「你周围」的氛围里猜有哪些地方
        places = [view.scene_names.get(s, s) for s in view.scenes]

        hp_cur, hp_max = view.player_hp
        player: dict = {"名字": view.player_name or "他"}
        if hp_max:
            player["血"] = f"{hp_cur}/{hp_max}"
            if hp_cur * 3 < hp_max:
                player["状态"] = "伤得不轻"
        if view.player_bag:
            # 同样是中文名：①要判断"他手上有没有能用的"，看到 p_pipe 只会瞎猜
            player["背包"] = [view.item_names.get(i, i) for i in view.player_bag]

        return {
            "你是谁": {
                "身份": persona.identity if persona else npc_id,
                "职责": list(persona.must) if persona else [],
                "她不会": list(persona.must_not) if persona else [],
                "说话方式": persona.tone if persona else "",
            },
            "你们的关系": STAGE_CN.get(stage.value, "陌生"),
            "现在": {
                "第几天": view.day,
                "时间": view.clock or self._clock.now().strftime("%H:%M"),
                "时段": view.slot or "",
                "她在": view.here or "不知道",
                "玩家在": view.player_at or "不知道",
                "体力": _stamina_cn(view.stamina if view.has_view else state.stamina),
                "情绪": _mood_cn(view.mood if view.has_view else state.mood),
                # 心情标签只在真有特殊心情时出现。空着就整项不写
                **({"心情": state.mood_text} if state.mood_text else {}),
            },
            "你周围": around,
            "能去的地方": places or ["（走不出去）"],
            "你身上": carried or ["没有东西"],
            "票据": view.money,
            "玩家": player,
            "剧情": {
                "第几章": view.story_chapter or "第一章",
                "进度": view.story_progress or "自由时间",
            },
            "待办约定": [m.value for m in unresolved],
            # 玩家画像：他说过的关于自己的事（喜欢什么、想要什么、家里有谁）。
            "玩家画像": [v for v in portrait if v][: self._cfg.decision_deep_k],
            # 「她记着的事」= 深刻记忆（一起经历过的、她为他做过的事）
            "她记着的事": [v for v in remembered if v][: self._cfg.decision_deep_k],
            # 「最近做过的事」= 动作流水
            "最近做过的事": [v for v in decided if v][: self._cfg.decision_event_k],
            # 「刚才没做成」只在真失败过时出现。这一栏是给她"接着处理"用的
            **({"刚才没做成": failed} if failed else {}),
        }

    # ── payload ──

    def _goal_payload(
        self,
        npc_id: str,
        profile: Profile,
        state: NpcState,
        view: WorldView,
        recent_items: list,
        *,
        pick: goal_lib.Pick,
    ) -> dict:
        payload = self._situation(npc_id, profile, state, view, recent_items)
        payload["当前目标"] = {
            "目标": pick.text,
            # pick.detail 在 LLM 模式下可能是模型补的那一句（"这次做两份"）
            "补充": pick.detail,
            "为什么是它": pick.because,
        }
        payload.update(self._abilities(view))
        return payload

    def _abilities(self, view: WorldView) -> dict:
        """「她此刻能做到什么程度」的三样事实。"""
        out: dict = {
            "现在能做出来的": {
                "菜": self._names(view, view.recipes_kitchen),
                "配件": self._names(view, view.recipes_storage),
            }
        }
        # 只在"真的该修"的时候才给这一项
        if view.repair_needed and view.equip:
            out["能修的装备"] = self._names(view, view.equip)
        if view.shop_items:
            out["货架上有的"] = self._names(view, view.shop_items)
        return out

    @staticmethod
    def _names(view: WorldView, ids) -> list[str]:
        return [view.item_names.get(i, i) for i in ids]

    def _tool_payload(
        self,
        npc_id: str,
        profile: Profile,
        view: WorldView,
        *,
        pick: goal_lib.Pick,
        steps: list,
    ) -> dict:
        """③ 的输入：只给"翻译需要的东西"。"""
        return {
            "当前目标": {"目标": pick.text, "补充": pick.detail},
            # 元素可能是 {"动作":"移动","地点":"后厨"}，也可能是兜底的人话（字符串）。
            "动作规划": list(steps),
            "她在哪": view.here or "不知道",
            "玩家在哪": view.player_at or "不知道",
            # 给的是资源不是可用动作：工具表在系统提示词里（全量）
            "当前资源": self._resources(view, npc_id, self._stage_of(profile, npc_id)),
        }

    def _resources(self, view: WorldView, npc_id: str, stage: Stage) -> dict:
        """这一刻真实可用的东西。"""

        def scenes(ids) -> list[str]:
            return [f"{i} {view.scene_names.get(i, '')}".strip() for i in ids]

        def items(ids, bag: dict[str, int] | None = None) -> list[str]:
            out = []
            for i in ids:
                label = f"{i} {view.item_names.get(i, '')}".strip()
                if bag and bag.get(i):
                    label += f"×{bag[i]}"
                out.append(label)
            return out

        return {
            "可去的地方": scenes(view.scenes),
            "她身上有的东西": items(sorted(view.bag), view.bag),
            "货架上有的东西": items(view.shop_items),
            "现在能做出来的菜": items(view.recipes_kitchen),
            "现在能做出来的配件": items(view.recipes_storage),
            "能修的装备": items(view.equip),
            # 背包 + 刚做出来的菜/配件。漏了后者，`cook → give` 这条链会被判错
            "能送出去的东西": items(
                sorted(set(view.bag) | set(view.recipes_kitchen) | set(view.recipes_storage)),
                view.bag,
            ),
            "能为他做的事": [f"{sid} {label}" for sid, label in view.care],
            "能发的邀请": list(view.invites),
            "说话主题": list(action_lib.TOPICS),
        }

    # ── 参数候选 ──

    def _options(
        self, npc_id: str, stage: Stage, view: WorldView
    ) -> dict[str, list[str]]:
        """全量工具的参数候选。"""
        out: dict[str, list[str]] = {}
        for a in action_lib.ACTIONS.values():
            for p in a.params:
                # 固定候选优先（修东西的 mode 这种），否则按局面算
                cands = (
                    list(p.choices) if p.choices
                    else self._resolve(p.source, view, npc_id, stage)
                )
                if cands is not None:
                    out[f"{a.id}.{p.name}"] = cands
        return out

    @staticmethod
    def _resolve(source: str, view: WorldView, npc_id: str, stage: Stage) -> list[str] | None:
        if not source:
            return None
        if source == action_lib.SRC_SCENE:
            return list(view.scenes)
        if source == action_lib.SRC_SCENE_QUIET:
            return list(view.quiet_scenes)
        if source == action_lib.SRC_ROOM:
            return [view.room] if view.room else None
        if source == action_lib.SRC_RECIPE_KITCHEN:
            return list(view.recipes_kitchen)
        if source == action_lib.SRC_RECIPE_STORAGE:
            return list(view.recipes_storage)
        if source == action_lib.SRC_ITEM_BAG:
            return sorted(view.bag)
        if source == action_lib.SRC_ITEM_SHOP:
            return list(view.shop_items)
        if source == action_lib.SRC_ITEM_FOOD:
            # 只给真能吃的。以前这里退到整包物品，于是「吃东西：电路板 / 废金属」——
            return sorted(set(view.food_items) | set(view.recipes_kitchen))
        if source == action_lib.SRC_ITEM_GIVE:
            # 和 _resources 里「能送出去的东西」是同一份：背包 + 能做出来的
            return sorted(set(view.bag) | set(view.recipes_kitchen) | set(view.recipes_storage))
        if source == action_lib.SRC_EQUIP:
            return list(view.equip)
        if source == action_lib.SRC_CARE:
            return [s.id for s in services.for_stage(npc_id, stage)]
        if source == action_lib.SRC_INVITE:
            return list(view.invites)
        if source == action_lib.SRC_TOPIC:
            return list(action_lib.TOPICS)
        return None

    def _value_pairs(
        self, npc_id: str, stage: Stage, view: WorldView
    ) -> dict[str, list[tuple[str, str]]]:
        """每个参数此刻的「中文名 → id」对照。"""
        care = {s.id: s.label for s in services.for_stage(npc_id, stage)}
        # 玩法层给的 label 更贴近她这一次真能做的（比如按场景过滤过），优先用它
        care.update({i: label for i, label in (view.care or ())})
        out: dict[str, list[tuple[str, str]]] = {}
        for a in action_lib.ACTIONS.values():
            for p in a.params:
                ids = (
                    list(p.choices) if p.choices
                    else self._resolve(p.source, view, npc_id, stage)
                )
                if ids is None:
                    continue
                seen: dict[str, str] = {}
                for i in ids:
                    cn = self._cn_value(p, i, view, care)
                    # 同名不同 id 时留第一个：中文值本来就分不开，认命
                    seen.setdefault(cn, i)
                out[f"{a.id}.{p.name}"] = list(seen.items())
        return out

    @staticmethod
    def _cn_value(p, value: str, view: WorldView, care: dict[str, str]) -> str:
        if p.choices or p.source == action_lib.SRC_TOPIC:
            return action_lib.VALUE_CN.get(value, value)
        if p.source in (
            action_lib.SRC_SCENE, action_lib.SRC_SCENE_QUIET, action_lib.SRC_ROOM
        ):
            return view.scene_names.get(value, value)
        if p.source == action_lib.SRC_CARE:
            return care.get(value, value)
        if p.source == action_lib.SRC_INVITE:
            return action_lib.VALUE_CN.get(value, value)
        return view.item_names.get(value, value)

    @staticmethod
    def _cn_options(
        pairs: dict[str, list[tuple[str, str]]]
    ) -> dict[str, list[str]]:
        return {k: [cn for cn, _ in v] for k, v in pairs.items()}

    # ── 模型两段 ──

    def _ask_text_plan(
        self,
        npc_id: str,
        pick: goal_lib.Pick,
        profile: Profile,
        state: NpcState,
        view: WorldView,
        recent_items: list,
        pairs: dict[str, list[tuple[str, str]]],
    ) -> list[dict] | None:
        """② 目标 → 中文动作串。"""
        payload = self._goal_payload(
            npc_id, profile, state, view, recent_items, pick=pick
        )
        schema = {
            "type": "object",
            "properties": {
                # 每条是一个对象：{"动作": "...", "<参数名>": "..."}。
                "plan": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["plan"],
        }
        raw = self._call(
            npc_id,
            build_text_plan_system(
                action_lib.describe_cn(self._cn_options(pairs), view.scene_names)
            ),
            payload,
            schema=schema,
            max_tokens=TEXT_PLAN_MAX_TOKENS,
            tag="打算",
        )
        if raw is None:
            return None
        return read_cn_steps(raw)

    def _ask_tools(
        self,
        npc_id: str,
        pick: goal_lib.Pick,
        steps: list,
        profile: Profile,
        view: WorldView,
        options: dict[str, list[str]],
    ) -> PlanResult | None:
        """③ 中文动作串 → 工具链。"""
        payload = self._tool_payload(npc_id, profile, view, pick=pick, steps=steps)
        # 一条动作 = act + 若干参数。参数键必须逐个声明。
        param_props = {name: {"type": "string"} for name in action_lib.param_names()}
        schema = {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"act": {"type": "string"}, **param_props},
                        "required": ["act"],
                    },
                }
            },
            "required": ["plan"],
        }
        raw = self._call(
            npc_id,
            # 全量工具表。不按目标裁剪——裁剪了就等于用规则替模型做规划。
            build_tool_system(action_lib.describe_all()),
            payload,
            schema=schema,
            max_tokens=TOOL_PLAN_MAX_TOKENS,
            tag="工具",
        )
        if raw is None:
            return None
        return parse_plan(raw, goal=pick.goal, options=options, room=view.room)

    def _call(
        self,
        npc_id: str,
        system: str,
        payload: dict,
        *,
        schema: dict,
        max_tokens: int,
        tag: str,
    ) -> dict | None:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            return self._llm.complete_json(
                messages, schema=schema, max_tokens=max_tokens
            )
        except LLMError as e:
            self._failures += 1
            self._last_error = str(e)[:160]
            print(f"[决策失败/{tag}] {npc_id}: {e}", flush=True)
            return None

    # ── 兜底 ──

    def _fallback(self, allowed: set[DecisionIntent], why: str) -> Decision:
        """不能用 idle 做默认兜底：idle 就是站着不动。"""
        for candidate in (DecisionIntent.WORK, DecisionIntent.SOLO):
            if candidate in allowed:
                goal = goal_lib.get(candidate.value)
                return Decision(
                    intent=candidate,
                    reason=why,
                    urgency=0.3,
                    goal_text=goal.want if goal else "",
                    plan_text=goal.steps if goal else (),
                )
        idle = goal_lib.get(DecisionIntent.IDLE.value)
        return Decision(
            intent=DecisionIntent.IDLE,
            reason=why,
            urgency=0.3,
            goal_text=idle.want if idle else "",
            plan_text=idle.steps if idle else (),
        )
