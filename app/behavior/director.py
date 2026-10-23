"""行为域：决策 → 行为脚本 → 行为树 → 执行。

脚本优先取 decision.script，其次按 intent 查 FALLBACK 模板，最后退到 idle。
模板里的 $room 在 build 时替换成该 NPC 的房间。
"""

from __future__ import annotations

import json

from ..domain.enums import DecisionIntent
from ..domain.models import Decision
from .behavior import Behavior, Handler
from .node import Node
from .parser import BehaviorScriptParser
from .runner import BehaviorRunner

# 每个 NPC 自己的房间（rest 用）
ROOMS: dict[str, str] = {"A": "lan_room", "B": "mio_room"}
DEFAULT_ROOM = "outdoor"

FALLBACK: dict[str, dict] = {
    DecisionIntent.IDLE.value: {"op": "idle"},
    # work 模板带移动，避免两个 NPC 同时静止。
    # 让它来回走两步、停一下，看起来像在忙自己的事。
    DecisionIntent.WORK.value: {
        "op": "repeat",
        "times": -1,
        "child": {
            "op": "seq",
            "children": [
                {"op": "wander"},
                {"op": "wait", "seconds": 3},
            ],
        },
    },
    DecisionIntent.SOLO.value: {
        "op": "seq",
        "children": [{"op": "move", "place": "?"}, {"op": "enjoy_view"}],
    },
    DecisionIntent.WITHDRAW.value: {"op": "rest"},
    DecisionIntent.REST.value: {
        "op": "seq",
        "children": [{"op": "move", "place": "$room"}, {"op": "rest"}],
    },
    DecisionIntent.SEEK_PLAYER.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "say", "topic": "greet"},
        ],
    },
    DecisionIntent.SHARE_MEMORY.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "say", "topic": "share_memory"},
        ],
    },
    DecisionIntent.INVITE_WALK.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "invite", "action": "invite_walk"},
        ],
    },
    DecisionIntent.INVITE_VISIT.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "invite", "action": "invite_visit"},
        ],
    },
    DecisionIntent.REQUEST_HUG.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player", "speed": 90},
            {"op": "invite", "action": "request_hug"},
        ],
    },
}

# 这些意图产生的是"默认状态"，不是"任务"。它们不会结束，
# 所以不能算 occupied，否则 NPC 一旦进入工作/独处/睡觉就再也不动了。
PASSIVE_INTENTS = {
    DecisionIntent.IDLE,
    DecisionIntent.WORK,
    DecisionIntent.SOLO,
    DecisionIntent.REST,       # 睡下之后 rest 永远 RUNNING，不 passive 就锁死
    DecisionIntent.WITHDRAW,
}


class BehaviorDirector:
    def __init__(self, parser: BehaviorScriptParser) -> None:
        self._parser = parser
        self._overrides: dict[str, Handler] = {}

    # ── 运行时改写 ──

    def override(self, op: str, handler: Handler) -> "BehaviorDirector":
        """按 op 名替换叶子的实现。解析后的树里所有同名叶子都会生效。"""
        self._overrides[op] = handler
        return self

    def clear_overrides(self) -> None:
        self._overrides.clear()

    # ── 主流程 ──

    def build(self, decision: Decision, npc_id: str = "A") -> Node:
        script = decision.script or FALLBACK.get(decision.intent.value, {"op": "idle"})
        script = _substitute(script, ROOMS.get(npc_id, DEFAULT_ROOM))
        tree = self._parser.parse(script)
        if self._overrides:
            for leaf in tree.leaves():
                handler = self._overrides.get(leaf.op)
                if handler is not None:
                    leaf.handler = handler
        return tree

    def apply(self, runner: BehaviorRunner, decision: Decision) -> Node:
        tree = self.build(decision, runner.agent.npc_id)
        runner.load(
            tree,
            label=decision.intent.value,
            passive=decision.intent in PASSIVE_INTENTS,
        )
        return tree

    def preview(self, decision: Decision, npc_id: str = "A") -> str:
        """调试面板用：把将要执行的流程打成可读的 DSL。"""
        return self.build(decision, npc_id).to_dsl()


def leaves_of(tree: Node) -> list[Behavior]:
    return list(tree.leaves())


def _substitute(script: dict, room: str) -> dict:
    """把模板里的 $room 换成该角色的房间。走一遍 JSON 最省事。"""
    if "$room" not in json.dumps(script):
        return script
    return json.loads(json.dumps(script).replace("$room", room))
