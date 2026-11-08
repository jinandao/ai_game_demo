"""俯视场景渲染。"""

from __future__ import annotations

import time
from collections.abc import Callable

import pygame

from ..behavior.world import Prop, Scene, World
from . import theme as T
from .assets import Assets

SCREEN_W, SCREEN_H = 1440, 810

# 0.1 是拖尾。0.22 走路时有跟随感、又不至于把人甩出画面。
CAM_LERP = 0.22

# 玩家在屏幕上的纵向位置：56% 而不是 50%——留一点上方视野
CAM_FOCUS_Y = 0.56

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
        self._encounter_hits: list[tuple[pygame.Rect, object]] = []
        self._hover_prop: str | None = None

        # 相机：世界坐标里的左上角
        self.cam: list[float] = [0.0, 0.0]
        self._scene_w = SCREEN_W
        self._scene_h = SCREEN_H
        # 整数偏移，画的时候统一用这个（浮点会让人物抖动）
        self._ox = 0
        self._oy = 0
        # 背景图按世界尺寸缩放过一次就缓存，不要每帧 transform
        self._bg_cache: dict[tuple[str, int, int], pygame.Surface] = {}

    # ── 相机 ──

    def follow(self, scene: Scene, target_xy: tuple[float, float]) -> None:
        """把相机对准玩家。场景比窗口小就居中（或贴左上）。"""
        self._scene_w, self._scene_h = scene.w, scene.h
        px, py = target_xy

        want_x = self._clamp_axis(px - SCREEN_W / 2, scene.w, SCREEN_W)
        want_y = self._clamp_axis(py - SCREEN_H * CAM_FOCUS_Y, scene.h, SCREEN_H)

        self.cam[0] += (want_x - self.cam[0]) * CAM_LERP
        self.cam[1] += (want_y - self.cam[1]) * CAM_LERP
        self._ox = int(self.cam[0])
        self._oy = int(self.cam[1])

    def snap(self, scene: Scene, target_xy: tuple[float, float]) -> None:
        """立刻对准（切场景时用，否则新场景的第一帧会从旧位置滑过来）。"""
        self._scene_w, self._scene_h = scene.w, scene.h
        px, py = target_xy
        self.cam[0] = self._clamp_axis(px - SCREEN_W / 2, scene.w, SCREEN_W)
        self.cam[1] = self._clamp_axis(py - SCREEN_H * CAM_FOCUS_Y, scene.h, SCREEN_H)
        self._ox = int(self.cam[0])
        self._oy = int(self.cam[1])

    @staticmethod
    def _clamp_axis(pos: float, world: int, view: int) -> float:
        if world <= view:
            return (world - view) / 2.0      # 世界比屏幕小：居中，两侧留黑边
        return max(0.0, min(world - view, pos))

    def to_world(self, pos: tuple[int, int]) -> tuple[float, float]:
        """屏幕坐标 -> 世界坐标。点空地走路时必须过这一层。"""
        return pos[0] + self.cam[0], pos[1] + self.cam[1]

    def to_screen(self, pos: tuple[float, float]) -> tuple[float, float]:
        return pos[0] - self.cam[0], pos[1] - self.cam[1]

    def visible_rect(self) -> tuple[float, float, float, float]:
        """当前可见的世界矩形 (x, y, w, h)。用来做视锥外的剔除。"""
        return self.cam[0], self.cam[1], SCREEN_W, SCREEN_H

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

    def encounter_at(self, pos: tuple[int, int]):
        """点到哪只怪。怪物画在最上层，命中也要优先。"""
        for box, enc in self._encounter_hits:
            if box.collidepoint(pos):
                return enc
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
        encounters=(),
        markers=frozenset(),
    ) -> None:
        scene = self.world.scene(scene_id)
        self._actor_hits.clear()
        self._prop_hits.clear()
        self._encounter_hits.clear()

        ox, oy = self._ox, self._oy
        has_bg = self._draw_background(surface, scene)

        # 室内背景通常已经把家具画进去了，这时不该再叠一层色块。
        bg_has_furniture = has_bg and scene.kind != "outdoor"

        visible = [a for a in agents if a.place == scene_id]
        drawables: list[tuple[float, Callable[[], None]]] = []

        # 视锥剔除：完全在屏幕外的物件不画，也不注册命中（点不到的东西不用判）
        view = pygame.Rect(ox - 200, oy - 300, SCREEN_W + 400, SCREEN_H + 600)

        for prop in scene.props:
            if not view.colliderect(pygame.Rect(*prop.rect)):
                continue
            drawables.append(
                (prop.y + prop.h, lambda p=prop: self._draw_prop(surface, p, bg_has_furniture))
            )
        for enc in encounters:
            if not view.colliderect(pygame.Rect(*enc.rect)):
                continue
            drawables.append((enc.y, lambda e=enc: self._draw_encounter(surface, e)))
        for agent in visible:
            drawables.append(
                (agent.y, lambda a=agent: self._draw_actor_with_marker(surface, a, markers))
            )
        drawables.append((player_xy[1], lambda: self._draw_player(surface, player_xy, player_name)))

        for _, fn in sorted(drawables, key=lambda x: x[0]):
            fn()

        self._draw_bubbles(surface, visible)

    def _draw_encounter(self, surface: pygame.Surface, enc) -> None:
        """怪物：一个色块 + 名字。够了，不需要精灵。"""
        box = pygame.Rect(enc.x - self._ox, enc.y - self._oy, enc.w, enc.h)
        # 命中框外扩 10px，和 NPC 一样的理由：小目标不好点
        self._encounter_hits.append((box.inflate(20, 20), enc))

        hover = box.collidepoint(pygame.mouse.get_pos())
        body = (150, 74, 68) if hover else (118, 62, 58)
        pygame.draw.rect(surface, body, box, border_radius=10)
        pygame.draw.rect(surface, T.DANGER if hover else (86, 46, 42), box, 2, border_radius=10)

        # 两只眼睛，让它像活物
        eye_y = box.y + box.h // 3
        for dx in (-12, 12):
            pygame.draw.circle(surface, (250, 226, 180), (box.centerx + dx, eye_y), 4)
            pygame.draw.circle(surface, (40, 20, 18), (box.centerx + dx, eye_y), 2)

        name = self.fonts.get(13).render(enc.name, True, T.TEXT)
        surface.blit(name, (box.centerx - name.get_width() // 2, box.y - 18))

    def _draw_background(self, surface: pygame.Surface, scene: Scene) -> bool:
        """返回是否用了真实背景图。"""
        ox, oy = self._ox, self._oy
        img = self.assets.background(scene.background) if self.assets else None
        if img is not None:
            key = (scene.background, scene.w, scene.h)
            cached = self._bg_cache.get(key)
            if cached is None:
                if img.get_size() != (scene.w, scene.h):
                    img = pygame.transform.scale(img, (scene.w, scene.h))
                cached = self._bg_cache[key] = img
            surface.blit(cached, (-ox, -oy))
            return True

        if scene.kind == "outdoor":
            surface.fill(T.GRASS)
            # 远处树林带：横跨整个世界
            pygame.draw.rect(surface, T.TREE_DARK, (-ox, -oy, scene.w, 300))
            pygame.draw.rect(surface, T.TREE_MID, (-ox, 250 - oy, scene.w, 70))
            # 主路：竖着一条，从树林带一直通到底
            road_x = scene.w // 2 - 150 - ox
            pygame.draw.rect(surface, T.PATH, (road_x, 300 - oy, 300, scene.h - 300))
            # 横向小路：连到两侧的建筑，让空地不要"一片同样的绿"
            pygame.draw.rect(surface, T.PATH, (-ox, 760 - oy, scene.w, 90))
            pygame.draw.rect(surface, T.PATH, (-ox, 1180 - oy, scene.w, 80))
        else:
            surface.fill(T.INDOOR_BG)
            # 地面：从墙脚线往下铺满整个世界
            pygame.draw.rect(surface, T.INDOOR_FLOOR, (-ox, scene.walk_top - oy, scene.w, scene.h))
            # 墙脚线
            pygame.draw.line(
                surface, T.INDOOR_LINE,
                (-ox, scene.walk_top - 2 - oy), (scene.w - ox, scene.walk_top - 2 - oy), 3,
            )

        return False

    def _draw_prop(
        self, surface: pygame.Surface, prop: Prop, bg_has_furniture: bool = False
    ) -> None:
        ox, oy = self._ox, self._oy
        box = pygame.Rect(prop.x - ox, prop.y - oy, prop.w, prop.h)
        self._prop_hits.append((box, prop))

        img = self.assets.prop(prop.id, prop.kind) if self.assets else None
        if img is not None:
            surface.blit(img, (box.x, box.y))
            return

        if prop.kind == "door":
            # 出口必须一直画出来——玩家被困在房间里是最难受的体验
            pygame.draw.rect(surface, T.INDOOR_LINE, box, border_radius=6)
            pygame.draw.rect(surface, T.DOOR, box.inflate(-14, -14), border_radius=4)
            label = self.fonts.get(14).render(prop.name, True, T.TEXT_FAINT)
            surface.blit(label, (box.centerx - label.get_width() // 2, box.y - 20))
            return

        if bg_has_furniture:
            return      # 背景里已经画了，不要再叠一个色块

        if prop.kind == "tree":
            cx = box.centerx
            pygame.draw.rect(surface, T.TREE_TRUNK, (cx - 7, box.y + 120, 14, 80))
            pygame.draw.polygon(
                surface,
                T.TREE_DARK,
                [(cx, box.y), (box.x, box.y + 150), (box.x + box.w, box.y + 150)],
            )
            pygame.draw.polygon(
                surface,
                T.TREE_MID,
                [
                    (cx, box.y + 30),
                    (box.x + 18, box.y + 120),
                    (box.x + box.w - 18, box.y + 120),
                ],
            )
            return

        if prop.kind == "water":
            pygame.draw.ellipse(surface, T.WATER, box)
            pygame.draw.ellipse(
                surface, T.WATER_HI, (box.x + 40, box.y + 40, box.w - 80, 60), 2
            )
            return

        if prop.kind == "building":
            body = pygame.Rect(box.x, box.y + 60, box.w, box.h - 60)
            pygame.draw.rect(surface, T.WALL, body)
            pygame.draw.polygon(
                surface,
                T.ROOF,
                [
                    (box.x - 10, box.y + 60),
                    (box.x + box.w + 10, box.y + 60),
                    (box.x + box.w - 20, box.y),
                    (box.x + 20, box.y),
                ],
            )
            door_w = 46
            door = pygame.Rect(body.centerx - door_w // 2, body.bottom - 56, door_w, 56)
            pygame.draw.rect(surface, T.DOOR, door)
            name = self.fonts.get(16).render(prop.name, True, T.TEXT_DIM)
            surface.blit(name, (body.centerx - name.get_width() // 2, body.top + 12))
            return

        # 普通摆设
        pygame.draw.rect(surface, T.PROP, box, border_radius=6)
        name = self.fonts.get(14).render(prop.name, True, T.TEXT_FAINT)
        surface.blit(name, (box.x + 8, box.y - 20))

    def _draw_actor_with_marker(self, surface: pygame.Surface, agent, markers) -> None:
        self._draw_actor(surface, agent)
        if agent.npc_id in markers:
            self._draw_marker(surface, agent)

    def _draw_marker(self, surface: pygame.Surface, agent) -> None:
        """头顶的感叹号：她这会儿有支线。"""
        cx, cy = int(agent.x) - self._ox, int(agent.y) - self._oy - 96
        top = cy
        pygame.draw.rect(surface, T.ACCENT, (cx - 3, top, 6, 17), border_radius=3)
        pygame.draw.circle(surface, T.ACCENT, (cx, top + 24), 4)
        pygame.draw.rect(surface, (60, 44, 26), (cx - 3, top, 6, 17), 1, border_radius=3)

    def _draw_actor(self, surface: pygame.Surface, agent) -> None:
        x, y = int(agent.x) - self._ox, int(agent.y) - self._oy
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
        """没有精灵表时的几何小人。"""
        body_color = T.NPC_A_BODY if agent.npc_id == "A" else T.NPC_B_BODY
        pygame.draw.rect(surface, body_color, (x - 11, y - 46, 22, 34), border_radius=6)
        pygame.draw.circle(surface, T.NPC_SKIN, (x, y - 56), 13)
        pygame.draw.circle(surface, T.NPC_HAIR, (x, y - 60), 13)
        # 命中框比人本身大一圈：世界放大之后，40×78 的小目标很难点中
        self._actor_hits[agent.npc_id] = pygame.Rect(x - 28, y - 82, 56, 92)

    def _draw_player(
        self, surface: pygame.Surface, xy: tuple[float, float], name: str
    ) -> None:
        x, y = int(xy[0]) - self._ox, int(xy[1]) - self._oy
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
            x = int(agent.x) - self._ox - width // 2
            x = max(8, min(x, SCREEN_W - width - 8))
            y = int(agent.y) - self._oy - 110 - height

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
