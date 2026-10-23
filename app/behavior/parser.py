"""行为域：行为脚本解析器，输入 JSON 或行式 DSL。

列表隐式转 seq，字符串转单个叶子，未知 op 降级为 idle 并记 warning。
"""

from __future__ import annotations

import re
from typing import Any

from .node import Node, Parallel, Repeat, Selector, Sequence
from .registry import BehaviorRegistry

COMBO_SEQ = {"seq", "sequence"}
COMBO_SEL = {"sel", "selector"}
COMBO_REP = {"repeat", "loop"}
COMBO_PAR = {"par", "parallel"}

# DSL 允许的裸值参数：move(outdoor) 等价于 move(place=outdoor)
DEFAULT_ARG = {
    "move": "place",
    "walk": "place",
    "say": "topic",
    "talk": "topic",
    "invite": "action",
    "wait": "seconds",
    "repeat": "times",
}

_HEAD = re.compile(r"^([A-Za-z_][\w]*)\s*(?:\((.*)\))?$")


class BehaviorScriptError(ValueError):
    pass


class BehaviorScriptParser:
    def __init__(self, registry: BehaviorRegistry) -> None:
        self._registry = registry
        self.warnings: list[str] = []

    # ── 入口 ──

    def parse(self, spec: Any) -> Node:
        self.warnings = []
        if isinstance(spec, dict) and "behavior" in spec:
            spec = spec["behavior"]
        node = self._build(spec)
        if node is None:
            self.warnings.append("脚本为空，降级为 idle")
            return self._fallback()
        return node

    # ── 结构 ──

    def _build(self, spec: Any) -> Node | None:
        if spec is None:
            return None

        if isinstance(spec, str):
            return self._build_dsl(spec)

        if isinstance(spec, list):
            children = [c for c in (self._build(x) for x in spec) if c is not None]
            return Sequence(children) if children else None

        if isinstance(spec, dict):
            op = str(spec.get("op") or spec.get("type") or "").strip()
            if not op:
                return None

            if op in COMBO_SEQ:
                return Sequence(self._children(spec))
            if op in COMBO_SEL:
                return Selector(self._children(spec))
            if op in COMBO_PAR:
                return Parallel(self._children(spec))
            if op in COMBO_REP:
                child = self._build(spec.get("child") or spec.get("children"))
                times = int(spec.get("times", -1))
                return Repeat(child, times) if child else None

            params = {
                k: v
                for k, v in spec.items()
                if k not in ("op", "type", "children", "child")
            }
            node = self._registry.create(op, params)
            if node is None:
                self.warnings.append(f"未知行为 op: {op!r}，已降级为 idle")
                return self._fallback()
            return node

        self.warnings.append(f"无法解析的节点: {spec!r}")
        return None

    def _children(self, spec: dict) -> list[Node]:
        raw = spec.get("children") or []
        if isinstance(raw, (str, dict)):
            raw = [raw]
        return [c for c in (self._build(x) for x in raw) if c is not None]

    def _fallback(self) -> Node:
        node = self._registry.create("idle", {})
        assert node is not None, "默认注册表里必须有 idle"
        return node

    # ── DSL ──

    def _build_dsl(self, text: str) -> Node | None:
        lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not lines:
            return None
        nodes, _ = self._parse_block(lines, 0)
        if not nodes:
            return None
        return nodes[0] if len(nodes) == 1 else Sequence(nodes)

    def _parse_block(self, lines: list[str], i: int) -> tuple[list[Node], int]:
        nodes: list[Node] = []
        while i < len(lines):
            line = lines[i]
            if line == "}":
                return nodes, i + 1

            if line.endswith("{"):
                head = line[:-1].strip()
                op, params = _parse_head(head)
                children, i = self._parse_block(lines, i + 1)
                nodes.append(self._make_combo(op, params, children))
                continue

            op, params = _parse_head(line)
            nodes.append(self._make_leaf(op, params))
            i += 1
        return nodes, i

    def _make_combo(self, op: str, params: dict[str, Any], children: list[Node]) -> Node:
        if op in COMBO_SEQ:
            return Sequence(children)
        if op in COMBO_SEL:
            return Selector(children)
        if op in COMBO_PAR:
            return Parallel(children)
        if op in COMBO_REP:
            times = int(params.get("times", -1))
            return Repeat(children[0], times) if children else self._fallback()
        self.warnings.append(f"未知组合子: {op!r}")
        return Sequence(children)

    def _make_leaf(self, op: str, params: dict[str, Any]) -> Node:
        node = self._registry.create(op, params)
        if node is None:
            self.warnings.append(f"未知行为 op: {op!r}，已降级为 idle")
            return self._fallback()
        return node


def _parse_head(head: str) -> tuple[str, dict[str, Any]]:
    m = _HEAD.match(head.strip())
    if not m:
        raise BehaviorScriptError(f"无法解析: {head!r}")
    op, raw = m.group(1), m.group(2)
    return op, _parse_args(op, raw)


def _parse_args(op: str, raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    out: dict[str, Any] = {}
    for part in _split_args(raw):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = _coerce(v.strip())
        else:
            out[DEFAULT_ARG.get(op, "value")] = _coerce(part.strip())
    return out


def _split_args(raw: str) -> list[str]:
    parts, buf, quote = [], [], ""
    for ch in raw:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _coerce(text: str) -> Any:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
