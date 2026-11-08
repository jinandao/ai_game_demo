"""把决策翻译成行为脚本。"""

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
    # serve 的具体动作（做哪件照料）由模型排，模板只能兜到「走过去问一句」
    DecisionIntent.SERVE.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "say", "topic": "care"},
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
    # ── 生产/商业/探索：这些意图的具体流程通常由模型排
    DecisionIntent.GATHER.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "ruins", "to_prop": "rubble"},
            {"op": "perform", "seconds": 8, "kind": "scavenge"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.PICK_FLOWER.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "garden", "to_prop": "flowers"},
            {"op": "perform", "seconds": 6, "kind": "pick_flower"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.COOK.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "kitchen", "to_prop": "stove_k"},
            {"op": "perform", "seconds": 12, "kind": "cook"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.CRAFT.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "storage", "to_prop": "table_s"},
            {"op": "perform", "seconds": 15, "kind": "craft"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.REPAIR.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "workshop", "to_prop": "workbench"},
            {"op": "perform", "seconds": 14, "kind": "repair"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.EAT.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "canteen", "to_prop": "table1"},
            {"op": "perform", "seconds": 10, "kind": "eat", "posture": "sit"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.SHOP_BUY.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "hall", "to_prop": "stall_food"},
            {"op": "perform", "seconds": 5, "kind": "browse"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.SHOP_SELL.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "hall", "to_prop": "counter"},
            {"op": "perform", "seconds": 5, "kind": "browse"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.SHIFT.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "hall", "to_prop": "notice_wall"},
            {"op": "perform", "seconds": 8, "kind": "take_job"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.TEND.value: {
        "op": "seq",
        "children": [
            {"op": "perform", "seconds": 10, "kind": "tend"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.PATROL.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "outdoor"},
            {"op": "perform", "seconds": 5, "kind": "scan"},
            {"op": "move", "place": "hall"},
            {"op": "perform", "seconds": 5, "kind": "scan"},
            {"op": "idle"},
        ],
    },
    DecisionIntent.SEARCH.value: {
        "op": "seq",
        "children": [
            {"op": "move", "place": "ruins", "to_prop": "rubble"},
            {"op": "perform", "seconds": 9, "kind": "loot"},
            {"op": "perform", "seconds": 9, "kind": "loot"},
            {"op": "idle"},
        ],
    },
}

# 这些意图产生的是"默认状态"，不是"任务"。它们不会结束
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
        return self.build_op(script, npc_id)

    def build_op(self, action: dict, npc_id: str = "A") -> Node:
        """把一条动作（或一小段脚本）建成树。"""
        script = _substitute(action, ROOMS.get(npc_id, DEFAULT_ROOM))
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
