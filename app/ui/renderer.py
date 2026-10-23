"""UI：俯视场景渲染，无资源时用色块与几何图形占位。"""

from __future__ import annotations

import time
from collections.abc import Callable

import pygame

from ..behavior.world import Prop, Scene, World
from . import theme as T
from .assets import Assets

SCREEN_W, SCREEN_H = 1440, 810


class SceneRenderer:
    def __init__(
        self,
        world: World,
        fonts: T.Fonts,
        names: dict[str, str] | None = None,
        assets: Assets | None = None,
    ) -> None:
        self.world = world
        self.fonts = fonts
        self.names = names or {}
        self.assets = assets

        self.bubbles: dict[str, tuple[str, float]] = {}
        self._actor_hits: dict[str, pygame.Rect] = {}
        # pygame.Rect 不可哈希，所以这里用列表存 (rect, prop)，不用 dict
        self._prop_hits: list[tuple[pygame.Rect, Prop]] = []
        self._hover_prop: str | None = None

    # ── 事件 ──

    def on_event(self, kind: str, npc_id: str, payload: dict) -> None:
        if kind in ("say", "invite"):
            text = (payload.get("text") or "").strip()
            if text:
                self.bubbles[npc_id] = (text, time.monotonic() + 6.0)

    def say(self, npc_id: str, text: str, hold: float = 6.0) -> None:
        if text.strip():
            self.bubbles[npc_id] = (text.strip(), time.monotonic() + hold)

    # ── 命中测试 ──

    def npc_at(self, pos: tuple[int, int]) -> str | None:
        for npc_id, box in self._actor_hits.items():
            if box.collidepoint(pos):
                return npc_id
        return None

    def prop_at(self, pos: tuple[int, int]) -> Prop | None:
        for box, prop in self._prop_hits:
            if box.collidepoint(pos):
                return prop
        return None

    # ── 绘制 ──

    def draw(
        self,
        surface: pygame.Surface,
        *,
        scene_id: str,
        agents,
        player_xy: tuple[float, float],
        player_name: str,
    ) -> None:
        scene = self.world.scene(scene_id)
        self._actor_hits.clear()
        self._prop_hits.clear()

        has_bg = self._draw_background(surface, scene)

        # 室内背景通常已经把家具画进去了，这时不该再叠一层色块。
        # 但点击区域仍要注册——不然玩家没法"看"那盏灯。
        bg_has_furniture = has_bg and scene.kind != "outdoor"

        visible = [a for a in agents if a.place == scene_id]
        drawables: list[tuple[float, Callable[[], None]]] = []
        for prop in scene.props:
            drawables.append(
                (prop.y + prop.h, lambda p=prop: self._draw_prop(surface, p, bg_has_furniture))
            )
        for agent in visible:
            drawables.append(
                (agent.y, lambda a=agent: self._draw_actor(surface, a))
            )
        drawables.append((player_xy[1], lambda: self._draw_player(surface, player_xy, player_name)))

        for _, fn in sorted(drawables, key=lambda x: x[0]):
            fn()

        self._draw_bubbles(surface, visible)

    def _draw_background(self, surface: pygame.Surface, scene: Scene) -> bool:
        """返回是否用了真实背景图。"""
        img = self.assets.background(scene.background) if self.assets else None
        if img is not None:
            if img.get_size() != (SCREEN_W, SCREEN_H):
                img = pygame.transform.scale(img, (SCREEN_W, SCREEN_H))
            surface.blit(img, (0, 0))
            return True

        if scene.kind == "outdoor":
            surface.fill(T.GRASS)
            # 远处树林带
            pygame.draw.rect(surface, T.TREE_DARK, (0, 0, SCREEN_W, 240))
            pygame.draw.rect(surface, T.TREE_MID, (0, 200, SCREEN_W, 70))
            # 小径
            pygame.draw.rect(surface, T.PATH, (520, 240, 380, SCREEN_H - 240))
        else:
            surface.fill(T.INDOOR_BG)
            # 地板
            pygame.draw.rect(surface, T.INDOOR_FLOOR, (0, 260, SCREEN_W, SCREEN_H - 260))
            # 墙脚线
            pygame.draw.line(surface, T.INDOOR_LINE, (0, 258), (SCREEN_W, 258), 3)

        return False

    def _draw_prop(
        self, surface: pygame.Surface, prop: Prop, bg_has_furniture: bool = False
    ) -> None:
        self._prop_hits.append((pygame.Rect(*prop.rect), prop))

        img = self.assets.prop(prop.id, prop.kind) if self.assets else None
        if img is not None:
            surface.blit(img, (prop.x, prop.y))
            return

        if prop.kind == "door":
            # 出口必须一直画出来——玩家被困在房间里是最难受的体验
            box = pygame.Rect(*prop.rect)
            pygame.draw.rect(surface, T.INDOOR_LINE, box, border_radius=6)
            pygame.draw.rect(surface, T.DOOR, box.inflate(-14, -14), border_radius=4)
            label = self.fonts.get(14).render(prop.name, True, T.TEXT_FAINT)
            surface.blit(
                label,
                (prop.x + prop.w // 2 - label.get_width() // 2, prop.y - 20),
            )
            return

        if bg_has_furniture:
            return      # 背景里已经画了，不要再叠一个色块

        if prop.kind == "tree":
            cx = prop.x + prop.w // 2
            pygame.draw.rect(surface, T.TREE_TRUNK, (cx - 7, prop.y + 120, 14, 80))
            pygame.draw.polygon(
                surface,
                T.TREE_DARK,
                [(cx, prop.y), (prop.x, prop.y + 150), (prop.x + prop.w, prop.y + 150)],
            )
            pygame.draw.polygon(
                surface,
                T.TREE_MID,
                [(cx, prop.y + 30), (prop.x + 18, prop.y + 120), (prop.x + prop.w - 18, prop.y + 120)],
            )
            return

        if prop.kind == "water":
            pygame.draw.ellipse(surface, T.WATER, prop.rect)
            pygame.draw.ellipse(surface, T.WATER_HI, (prop.x + 40, prop.y + 40, prop.w - 80, 60), 2)
            return

        if prop.kind == "building":
            body = pygame.Rect(prop.x, prop.y + 60, prop.w, prop.h - 60)
            pygame.draw.rect(surface, T.WALL, body)
            pygame.draw.polygon(
                surface,
                T.ROOF,
                [
                    (prop.x - 10, prop.y + 60),
                    (prop.x + prop.w + 10, prop.y + 60),
                    (prop.x + prop.w - 20, prop.y),
                    (prop.x + 20, prop.y),
                ],
            )
            door_w = 46
            door = pygame.Rect(prop.x + prop.w // 2 - door_w // 2, body.bottom - 56, door_w, 56)
            pygame.draw.rect(surface, T.DOOR, door)
            name = self.fonts.get(16).render(prop.name, True, T.TEXT_DIM)
            surface.blit(name, (prop.x + prop.w // 2 - name.get_width() // 2, body.top + 12))
            return

        # 普通摆设
        pygame.draw.rect(surface, T.PROP, prop.rect, border_radius=6)
        name = self.fonts.get(14).render(prop.name, True, T.TEXT_FAINT)
        surface.blit(name, (prop.x + 8, prop.y - 20))

    def _draw_actor(self, surface: pygame.Surface, agent) -> None:
        x, y = int(agent.x), int(agent.y)
        name = self.names.get(agent.npc_id, agent.npc_id)

        # 阴影：有没有精灵表都画
        pygame.draw.ellipse(surface, T.SHADOW, (x - 18, y - 6, 36, 12))

        sheet = self.assets.sprite(agent.npc_id) if self.assets else None
        if sheet is not None:
            img = sheet.frame(agent.facing, agent.walking, time.monotonic())
            rect = img.get_rect(midbottom=(x, y))
            surface.blit(img, rect)
            self._actor_hits[agent.npc_id] = pygame.Rect(
                x - 24, y - sheet.frame_h, 48, sheet.frame_h + 8
            )
        else:
            self._draw_placeholder_actor(surface, x, y, agent)

        label = self.fonts.get(15, bold=True).render(name, True, T.TEXT)
        surface.blit(label, (x - label.get_width() // 2, y - 92))

        state = _state_text(agent)
        if state:
            tag = self.fonts.get(13).render(state, True, T.TEXT_FAINT)
            surface.blit(tag, (x - tag.get_width() // 2, y + 8))

    def _draw_placeholder_actor(
        self, surface: pygame.Surface, x: int, y: int, agent
    ) -> None:
        """没有精灵表时的几何小人。有图之后这段不会被调用，但也不能删——
        美术可能只做了一半，缺谁就退谁。"""
        body_color = T.NPC_A_BODY if agent.npc_id == "A" else T.NPC_B_BODY
        pygame.draw.rect(surface, body_color, (x - 11, y - 46, 22, 34), border_radius=6)
        pygame.draw.circle(surface, T.NPC_SKIN, (x, y - 56), 13)
        pygame.draw.circle(surface, T.NPC_HAIR, (x, y - 60), 13)
        self._actor_hits[agent.npc_id] = pygame.Rect(x - 20, y - 72, 40, 78)

    def _draw_player(
        self, surface: pygame.Surface, xy: tuple[float, float], name: str
    ) -> None:
        x, y = int(xy[0]), int(xy[1])
        pygame.draw.ellipse(surface, T.SHADOW, (x - 16, y - 5, 32, 10))
        pygame.draw.rect(surface, T.PLAYER_BODY, (x - 10, y - 42, 20, 32), border_radius=5)
        pygame.draw.circle(surface, T.NPC_SKIN, (x, y - 52), 12)
        pygame.draw.circle(surface, T.PLAYER_HAIR, (x, y - 56), 12)

        label = self.fonts.get(14).render(name, True, T.PLAYER_BODY)
        surface.blit(label, (x - label.get_width() // 2, y - 86))

    def _draw_bubbles(self, surface: pygame.Surface, agents) -> None:
        font = self.fonts.get(17)
        now = time.monotonic()

        for agent in agents:
            entry = self.bubbles.get(agent.npc_id)
            if not entry:
                continue
            text, expire = entry
            if now > expire:
                self.bubbles.pop(agent.npc_id, None)
                continue

            lines = wrap_text(text, font, 320)[:4]
            if not lines:
                continue

            width = max(font.size(line)[0] for line in lines) + 24
            height = len(lines) * 25 + 16
            x = int(agent.x) - width // 2
            x = max(8, min(x, SCREEN_W - width - 8))
            y = int(agent.y) - 110 - height

            box = pygame.Surface((width, height), pygame.SRCALPHA)
            pygame.draw.rect(box, (18, 21, 26, 235), box.get_rect(), border_radius=10)
            pygame.draw.rect(box, (*T.PANEL_LINE, 255), box.get_rect(), 1, border_radius=10)
            for i, line in enumerate(lines):
                box.blit(font.render(line, True, T.TEXT), (12, 9 + i * 25))
            surface.blit(box, (x, y))

            pygame.draw.polygon(
                surface,
                (18, 21, 26),
                [
                    (x + width // 2 - 7, y + height),
                    (x + width // 2 + 7, y + height),
                    (x + width // 2, y + height + 8),
                ],
            )


def _state_text(agent) -> str:
    if agent.posture == "lie":
        return "睡着"
    if agent.posture == "sit":
        return "坐着"
    if agent.walking:
        return "走过去"
    if agent.busy:
        return "在忙"
    return ""


def wrap_text(text: str, font: pygame.font.Font, max_width: int) -> list[str]:
    """按宽度折行。中文没有空格，所以按字符累加。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        if font.size(current + ch)[0] > max_width and current:
            lines.append(current)
            current = ch
        else:
            current += ch
    if current:
        lines.append(current)
    return lines
