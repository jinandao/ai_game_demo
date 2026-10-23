"""UI：对话界面，左对话栏、右立绘、底部输入。"""

from __future__ import annotations

import pygame

from . import theme as T
from .assets import Assets
from .renderer import SCREEN_W, SCREEN_H, wrap_text

HEADER_H = 48
CHAT_W = 900
PORTRAIT_X = 900
PORTRAIT_W = 540
CHAT_TOP = HEADER_H
CHAT_H = 662
ACTION_Y = 710
INPUT_Y = 762
INPUT_H = 40
INPUT_W = 1240
SEND_X = 1274
SEND_W = 146


class ChatPanel:
    def __init__(self, fonts: T.Fonts, assets: Assets | None = None) -> None:
        self.fonts = fonts
        self.assets = assets
        self.npc_id: str | None = None
        self.npc_name: str = ""
        self.messages: list[list[str]] = []
        self.input_text = ""
        self.busy = False
        self.actions: list[tuple[str, str]] = []
        self.emotion = "calm"          # calm | soft

        # 输入框焦点。打开对话即聚焦——进来的目的就是说话，
        # 还要再点一下才能打字是没必要的门槛。
        self.focused = False
        self._textinput_ok = False     # 收到过 TEXTINPUT 就说明 IME 通道是通的
        self._streaming = False        # 正在往最后一条气泡里追加文字
        self.composition = ""          # 输入法正在组词的内容（"ni hao" 或待选字）

        self._scroll = 0.0
        self._max_scroll = 0.0
        self._hover: str | None = None
        self._hotspots: dict[str, pygame.Rect] = {}

    # ── 状态 ──

    def open(self, npc_id: str, npc_name: str) -> None:
        self.npc_id = npc_id
        self.npc_name = npc_name
        self.messages = []
        self.input_text = ""
        self.busy = False
        self.emotion = "calm"
        self.focused = True            # 进对话就能直接打字
        self.composition = ""
        self._scroll = 0.0
        self._streaming = False
        pygame.key.start_text_input()

    def close(self) -> None:
        self.npc_id = None
        self.messages = []
        self.input_text = ""
        self.composition = ""
        self.busy = False
        self.focused = False
        self._streaming = False
        pygame.key.stop_text_input()

    @property
    def is_open(self) -> bool:
        return self.npc_id is not None

    def add(self, role: str, text: str) -> None:
        at_bottom = self._scroll >= self._max_scroll - 4
        self.messages.append([role, text])
        if len(self.messages) > 300:
            del self.messages[:60]
        if at_bottom:
            self._scroll = 1e9      # 下一帧 clamp 到底

    # ── 流式 ──
    # 她的话是一边生成一边显示的。先占一个空气泡，后面往里追加，
    # 结束后用最终清洗过的文本覆盖一次（banned 词、小说体清洗都在那时候做）。

    def begin_stream(self, role: str = "npc") -> None:
        self.messages.append([role, ""])
        self._streaming = True
        self._scroll = 1e9

    def append_stream(self, chunk: str) -> None:
        if not self._streaming or not self.messages:
            return
        self.messages[-1][1] += chunk
        self._scroll = 1e9

    def end_stream(self, final_text: str | None = None) -> None:
        if not self._streaming:
            return
        self._streaming = False
        if not self.messages:
            return
        if final_text is not None:
            self.messages[-1][1] = final_text
        if not self.messages[-1][1].strip():
            self.messages.pop()      # 空回复不要留一个空气泡

    def set_busy(self, busy: bool) -> None:
        self.busy = busy

    def set_actions(self, actions: list[tuple[str, str]]) -> None:
        self.actions = actions

    def set_emotion(self, emotion: str) -> None:
        self.emotion = emotion

    # ── 事件 ──

    def handle_event(self, event: pygame.event.Event) -> tuple | None:
        if not self.is_open:
            return None

        if event.type == pygame.TEXTEDITING:
            # 输入法组的词还没上屏。系统候选框由 SDL 负责显示（可能不显示），
            # 这里把它同时画进输入框——至少玩家能看到自己在打什么。
            self.composition = event.text
            return None

        if event.type == pygame.TEXTINPUT:
            self._textinput_ok = True          # IME 通道是通的
            self.composition = ""
            self.input_text += event.text
            return None

        if event.type == pygame.KEYDOWN:
            # TEXTINPUT 没来的环境（某些 SDL2 构建 / 无 IME 的窗口）靠这里兜底。
            # 只在确认 TEXTINPUT 不工作时才用，否则一个字符会被加两遍。
            if not self._textinput_ok and event.unicode and event.unicode.isprintable():
                if event.mod & (pygame.KMOD_CTRL | pygame.KMOD_ALT):
                    return None                # Ctrl+A 之类不要打进输入框
                self.input_text += event.unicode
                return None

            if event.key == pygame.K_BACKSPACE:
                self.input_text = self.input_text[:-1]
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                text = self.input_text.strip()
                if text and not self.busy:
                    self.input_text = ""
                    self.composition = ""
                    return ("send", text)
            elif event.key == pygame.K_ESCAPE:
                return ("end",)
            elif event.key == pygame.K_PAGEUP:
                self._scroll = max(0.0, self._scroll - CHAT_H * 0.8)
            elif event.key == pygame.K_PAGEDOWN:
                self._scroll = min(self._max_scroll, self._scroll + CHAT_H * 0.8)
            return None

        if event.type == pygame.MOUSEWHEEL:
            self._scroll = max(0.0, min(self._max_scroll, self._scroll - event.y * 60))
            return None

        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for key, box in self._hotspots.items():
                if not box.collidepoint(event.pos):
                    continue

                if key == "input":
                    self.focused = True
                    pygame.key.start_text_input()   # 重新点一下也重新激活 IME
                    return None

                self.focused = False
                if key == "end":
                    return ("end",)
                if key == "send":
                    text = self.input_text.strip()
                    if text and not self.busy:
                        self.input_text = ""
                        return ("send", text)
                    return None
                if key.startswith("act:"):
                    return ("action", key[4:])

            self.focused = False            # 点空白处失焦
            return None

        if event.type == pygame.MOUSEMOTION:
            self._hover = None
            for key, box in self._hotspots.items():
                if box.collidepoint(event.pos):
                    self._hover = key
                    break
        return None

    # ── 绘制 ──

    def draw(self, surface: pygame.Surface) -> None:
        if not self.is_open:
            return

        pygame.draw.rect(surface, T.PANEL_BG, (0, 0, SCREEN_W, SCREEN_H))
        self._hotspots = {}

        self._draw_header(surface)
        self._draw_messages(surface)
        self._draw_portrait(surface)
        self._draw_actions(surface)
        self._draw_input(surface)

    def _draw_header(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(surface, (25, 29, 36), (0, 0, SCREEN_W, HEADER_H))
        pygame.draw.line(surface, T.PANEL_LINE, (0, HEADER_H), (SCREEN_W, HEADER_H), 1)

        name = self.fonts.get(22, bold=True).render(self.npc_name, True, T.TEXT)
        surface.blit(name, (28, 11))

        status = "正在想…" if self.busy else "在听"
        tag = self.fonts.get(15).render(status, True, T.TEXT_FAINT)
        surface.blit(tag, (28 + name.get_width() + 16, 17))

        btn = pygame.Rect(SCREEN_W - 132, 9, 108, 30)
        hovered = self._hover == "end"
        pygame.draw.rect(surface, (60, 36, 36) if hovered else (40, 34, 36), btn, border_radius=6)
        pygame.draw.rect(surface, T.PANEL_LINE, btn, 1, border_radius=6)
        label = self.fonts.get(15).render("结束会话", True, T.TEXT_DIM)
        surface.blit(label, (btn.centerx - label.get_width() // 2, btn.centery - 9))
        self._hotspots["end"] = btn

    def _draw_messages(self, surface: pygame.Surface) -> None:
        area = pygame.Rect(0, CHAT_TOP, CHAT_W, CHAT_H)
        pygame.draw.rect(surface, (24, 27, 33), area)
        surface.set_clip(area)

        font = self.fonts.get(18)
        blocks = self._layout(font, CHAT_W - 60)
        content_h = blocks[-1][2] + blocks[-1][3] + 24 if blocks else 0
        self._max_scroll = max(0.0, content_h - CHAT_H)
        self._scroll = min(self._scroll, self._max_scroll)

        for role, lines, top, height in blocks:
            y = area.top + 16 + top - self._scroll
            if y + height < area.top or y > area.bottom:
                continue
            self._draw_bubble(surface, area, role, lines, y, height)

        if not blocks:
            hint = self.fonts.get(17).render("说点什么吧。", True, T.TEXT_FAINT)
            surface.blit(hint, (28, area.top + 24))

        if self.busy:
            dots = self.fonts.get(17).render("……", True, T.TEXT_FAINT)
            surface.blit(dots, (28, area.bottom - 34))

        surface.set_clip(None)
        self._draw_scrollbar(surface, area, content_h)

    def _layout(self, font, width) -> list[tuple[str, list[str], int, int]]:
        blocks = []
        y = 0
        for role, text in self.messages:
            lines = wrap_text(text, font, width) or [""]
            height = len(lines) * 26 + 26
            blocks.append((role, lines, y, height))
            y += height + 12
        return blocks

    def _draw_bubble(self, surface, area, role, lines, y, height) -> None:
        font = self.fonts.get(18)
        is_player = role == "player"
        block_w = min(max(font.size(line)[0] for line in lines) + 36, CHAT_W - 120)
        x = area.right - 30 - block_w if is_player else 28
        box = pygame.Rect(x, y, block_w, height)

        fill = (38, 48, 42) if is_player else (32, 36, 44)
        pygame.draw.rect(surface, fill, box, border_radius=10)
        if is_player:
            pygame.draw.rect(surface, (52, 68, 58), box, 1, border_radius=10)

        for i, line in enumerate(lines):
            color = T.PLAYER_TEXT if is_player else T.TEXT
            surface.blit(font.render(line, True, color), (box.left + 18, box.top + 12 + i * 26))

    def _draw_scrollbar(self, surface, area, content_h) -> None:
        if content_h <= CHAT_H:
            return
        track = pygame.Rect(CHAT_W - 8, area.top + 6, 5, CHAT_H - 12)
        pygame.draw.rect(surface, (36, 40, 48), track, border_radius=3)
        ratio = CHAT_H / content_h
        thumb_h = max(30, int(track.height * ratio))
        pos = self._scroll / max(1.0, self._max_scroll)
        thumb_y = track.top + int((track.height - thumb_h) * pos)
        pygame.draw.rect(
            surface, (90, 96, 106), (track.left, thumb_y, 5, thumb_h), border_radius=3
        )

    def _draw_portrait(self, surface: pygame.Surface) -> None:
        area = pygame.Rect(PORTRAIT_X, CHAT_TOP, PORTRAIT_W, CHAT_H)

        # 背景：一点冷色渐变，把立绘托起来
        for i in range(0, area.height, 4):
            k = i / area.height
            color = (
                int(26 + 8 * k),
                int(29 + 8 * k),
                int(34 + 10 * k),
            )
            pygame.draw.rect(surface, color, (area.left, area.top + i, area.width, 4))

        img = self.assets.portrait(self.npc_id, self.emotion) if self.assets else None
        if img is not None:
            rect = img.get_rect(midbottom=(area.centerx, area.bottom))
            surface.blit(img, rect)
            return

        cx = area.centerx
        bottom = area.bottom

        # 肩 / 身体
        body = [
            (cx - 190, bottom),
            (cx - 150, bottom - 230),
            (cx - 70, bottom - 300),
            (cx + 70, bottom - 300),
            (cx + 150, bottom - 230),
            (cx + 190, bottom),
        ]
        body_color = (58, 63, 69) if self.npc_id == "A" else (232, 230, 222)
        pygame.draw.polygon(surface, body_color, body)

        # 脖子
        pygame.draw.rect(surface, T.NPC_SKIN, (cx - 32, bottom - 360, 64, 80))
        # 头
        pygame.draw.ellipse(surface, T.NPC_SKIN, (cx - 92, bottom - 520, 184, 210))
        # 头发
        hair = T.NPC_HAIR if self.npc_id == "A" else (32, 30, 34)
        pygame.draw.ellipse(surface, hair, (cx - 100, bottom - 540, 200, 190))
        pygame.draw.rect(surface, T.NPC_SKIN, (cx - 92, bottom - 430, 184, 120))

        # 眼睛：平静是点，soft 是弯线
        eye_y = bottom - 400
        if self.emotion == "soft":
            pygame.draw.arc(surface, T.NPC_HAIR, (cx - 62, eye_y - 6, 44, 26), 3.4, 6.1, 3)
            pygame.draw.arc(surface, T.NPC_HAIR, (cx + 18, eye_y - 6, 44, 26), 3.4, 6.1, 3)
            # 腮红
            blush = pygame.Surface((160, 60), pygame.SRCALPHA)
            pygame.draw.ellipse(blush, (226, 132, 132, 70), (10, 10, 50, 24))
            pygame.draw.ellipse(blush, (226, 132, 132, 70), (100, 10, 50, 24))
            surface.blit(blush, (cx - 80, eye_y + 20))
        else:
            pygame.draw.ellipse(surface, T.NPC_HAIR, (cx - 56, eye_y, 24, 18))
            pygame.draw.ellipse(surface, T.NPC_HAIR, (cx + 32, eye_y, 24, 18))

        # 嘴
        pygame.draw.line(
            surface, (150, 110, 100), (cx - 16, eye_y + 66), (cx + 16, eye_y + 66), 3
        )

        tag = self.fonts.get(13).render("立绘占位 · 待替换 PNG", True, T.TEXT_FAINT)
        surface.blit(tag, (area.right - tag.get_width() - 14, area.bottom - 24))

    def _draw_actions(self, surface: pygame.Surface) -> None:
        if not self.actions:
            return
        font = self.fonts.get(16)
        x = 24
        for action_id, label in self.actions:
            w = font.size(label)[0] + 34
            box = pygame.Rect(x, ACTION_Y, w, 34)
            hovered = self._hover == f"act:{action_id}"
            pygame.draw.rect(
                surface, (58, 48, 36) if hovered else (36, 34, 31), box, border_radius=17
            )
            pygame.draw.rect(surface, T.ACCENT_DIM, box, 1, border_radius=17)
            text = font.render(label, True, T.ACCENT if hovered else T.TEXT_DIM)
            surface.blit(text, (box.centerx - text.get_width() // 2, box.centery - 10))
            self._hotspots[f"act:{action_id}"] = box
            x += w + 12

    def _draw_input(self, surface: pygame.Surface) -> None:
        box = pygame.Rect(20, INPUT_Y, INPUT_W, INPUT_H)
        focused = self.focused and not self.busy

        pygame.draw.rect(surface, (30, 34, 42), box, border_radius=8)
        pygame.draw.rect(
            surface, T.ACCENT_DIM if focused else T.PANEL_LINE, box, 2 if focused else 1,
            border_radius=8,
        )
        self._hotspots["input"] = box      # 点一下就能聚焦

        # 告诉系统输入法"输入框在这里"，候选词窗口才会贴到正确的位置。
        # 不设的话，Windows 上候选框可能跑到窗口外面，看起来就是"没有候选框"。
        set_rect = getattr(pygame.key, "set_text_input_rect", None)
        if set_rect is not None:
            set_rect(box)

        font = self.fonts.get(18)
        text_left = box.left + 16
        text_top = box.top + 10
        cursor_on = (pygame.time.get_ticks() // 500) % 2 == 0

        if self.busy:
            hint = font.render("（她还没说完）", True, T.TEXT_FAINT)
            surface.blit(hint, (text_left, text_top))
        elif not self.input_text and not self.composition and not focused:
            # 只有没聚焦时才显示占位符：聚焦后换成光标，否则看不出"能打字了"
            hint = font.render("说点什么…（回车发送）", True, T.TEXT_FAINT)
            surface.blit(hint, (text_left, text_top))
        else:
            # 已确定的文字 + 输入法正在组的词，分成两段画：
            # 后者用弱色和下划线，让"还没上屏"和"已经输入"看起来不一样
            shown, comp = self.input_text, self.composition
            limit = box.width - 40
            while shown and font.size(shown + comp)[0] > limit:
                shown = shown[1:]
            while comp and font.size(shown + comp)[0] > limit:
                comp = comp[1:]

            x = text_left
            if shown:
                t = font.render(shown, True, T.TEXT)
                surface.blit(t, (x, text_top))
                x += t.get_width()
            if comp:
                c = font.render(comp, True, T.TEXT_DIM)
                surface.blit(c, (x, text_top))
                pygame.draw.line(
                    surface, T.TEXT_DIM, (x, text_top + 25), (x + c.get_width(), text_top + 25), 1
                )
                x += c.get_width()

            if focused and cursor_on:
                pygame.draw.line(surface, T.TEXT, (x + 2, box.top + 11), (x + 2, box.top + 30), 2)

        send = pygame.Rect(SEND_X, INPUT_Y, SEND_W, INPUT_H)
        hovered = self._hover == "send"
        pygame.draw.rect(surface, (52, 60, 54) if hovered else (38, 44, 40), send, border_radius=8)
        pygame.draw.rect(surface, T.PANEL_LINE, send, 1, border_radius=8)
        label = self.fonts.get(16).render("发送", True, T.TEXT_DIM)
        surface.blit(label, (send.centerx - label.get_width() // 2, send.centery - 9))
        self._hotspots["send"] = send
