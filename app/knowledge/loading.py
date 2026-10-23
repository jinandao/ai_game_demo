"""知识域：从 markdown 加载资料库，按 ## 切块。

文件名含 _private 的块仅本人可检索；*_vocab.md 不进库，只拼进提示词。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..domain.enums import Scope
from .base import Chunk

_NPC = re.compile(r"^npc_(?P<npc>[A-Za-z0-9]+)_(?P<idx>\d+)_(?P<slug>[a-z_]+)\.md$")

# 这些文件只是人看的参考资料，不进检索库
_SKIP_SLUGS = {"vocab"}


def load_kb(directory: Path) -> list[Chunk]:
    """目录不存在时返回空列表，不抛异常——demo 不应该因为缺 kb 起不来。"""
    if not directory.exists():
        return []

    chunks: list[Chunk] = []
    for path in sorted(directory.rglob("*.md")):
        rel = path.relative_to(directory)
        scope, owner, visibility = _classify(rel)
        if scope is None:
            continue
        chunks.extend(_split(path, rel, scope, owner, visibility))
    return chunks


def _classify(rel: Path) -> tuple[Scope | None, str | None, str]:
    name = rel.name

    m = _NPC.match(name)
    if m:
        if m.group("slug") in _SKIP_SLUGS:
            return None, None, "public"
        private = "private" in m.group("slug")
        return Scope.CHARACTER, m.group("npc"), "private" if private else "public"

    if name == "places.md":
        return Scope.WORLD, None, "public"

    if "world" in rel.parts:
        return Scope.WORLD, None, "public"

    return None, None, "public"


def _split(
    path: Path,
    rel: Path,
    scope: Scope,
    owner: str | None,
    visibility: str,
) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"^##\s+", text, flags=re.MULTILINE)

    stem = rel.with_suffix("").as_posix().replace("/", ".")
    out: list[Chunk] = []

    for i, block in enumerate(blocks[1:], start=1):   # blocks[0] 是一级标题前的引言
        lines = block.strip().splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not body:
            continue
        out.append(
            Chunk(
                id=f"{stem}#{i}",
                text=f"{title}：{body}",
                scope=scope,
                owner=owner,
                visibility=visibility,
                tags=_tags(title, body),
            )
        )
    return out


def _tags(title: str, body: str) -> tuple[str, ...]:
    """把标题里出现的实词当标签，提升关键词检索的命中率。"""
    words = re.findall(r"[\u4e00-\u9fff]{2,4}", title)
    return tuple(dict.fromkeys(words))[:4]
