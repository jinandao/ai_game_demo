"""UI：可选美术资源，缺失时由渲染层用几何占位。

命名约定：assets/bg_{scene}.png、prop_{id}.png、spr_{npc}.png、portrait_{npc}_{emotion}.png。
"""

from __future__ import annotations

from pathlib import Path

import pygame

FACING_ROW = {"down": 0, "left": 1, "right": 2, "up": 3}

DEFAULT_FRAME_W = 48
DEFAULT_FRAME_H = 72
WALK_FPS = 8.0


class SpriteSheet:
    """精灵表：行是朝向，列是帧，第 0 列站立，其余行走帧。"""

    def __init__(
        self,
        image: pygame.Surface,
        frame_w: int = DEFAULT_FRAME_W,
        frame_h: int = DEFAULT_FRAME_H,
    ) -> None:
        self.image = image
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.cols = max(1, image.get_width() // frame_w)
        self.rows = max(1, image.get_height() // frame_h)

    def frame(self, facing: str, walking: bool, t: float) -> pygame.Surface:
        row = FACING_ROW.get(facing, 0)
        if self.rows == 1 or row >= self.rows:
            row = 0                      # 只有一行时所有朝向都用它

        if not walking or self.cols <= 1:
            col = 0                      # 站立
        else:
            col = int(t * WALK_FPS) % (self.cols - 1) + 1

        rect = pygame.Rect(
            col * self.frame_w, row * self.frame_h, self.frame_w, self.frame_h
        )
        try:
            return self.image.subsurface(rect)
        except ValueError:
            # 表尺寸和预期不符时退回左上角那一帧，不要崩
            return self.image.subsurface(
                pygame.Rect(0, 0, self.frame_w, self.frame_h)
            )


class Assets:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self._hit: dict[str, pygame.Surface] = {}
        self._miss: set[str] = set()

    # ── 通用 ──

    def image(self, rel: str) -> pygame.Surface | None:
        if rel in self._hit:
            return self._hit[rel]
        if rel in self._miss:
            return None

        path = self.root / rel
        if not path.exists():
            self._miss.add(rel)
            return None

        try:
            img = pygame.image.load(str(path)).convert_alpha()
        except (pygame.error, OSError):
            self._miss.add(rel)
            return None

        self._hit[rel] = img
        return img

    # ── 语义化取图 ──

    def background(self, rel: str) -> pygame.Surface | None:
        return self.image(rel)

    def prop(self, prop_id: str, kind: str = "") -> pygame.Surface | None:
        """先找单件图，再退同类图。都没有就返回 None（渲染层画几何）。"""
        img = self.image(f"assets/prop_{prop_id}.png")
        if img is None and kind:
            img = self.image(f"assets/prop_{kind}.png")
        return img

    def sprite(self, npc_id: str) -> SpriteSheet | None:
        img = self.image(f"assets/spr_{npc_id}.png")
        if img is None:
            return None
        return SpriteSheet(img)

    def portrait(self, npc_id: str, emotion: str = "calm") -> pygame.Surface | None:
        img = self.image(f"assets/portrait_{npc_id}_{emotion}.png")
        if img is None and emotion != "calm":
            img = self.image(f"assets/portrait_{npc_id}_calm.png")
        return img

    # ── 调试 ──

    def stats(self) -> dict[str, int]:
        return {"loaded": len(self._hit), "missing": len(self._miss)}
