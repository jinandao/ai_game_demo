"""UI：主题与字体。"""

from __future__ import annotations

import sys
from pathlib import Path

import pygame

# ── 户外 ──
GRASS = (127, 191, 106)
GRASS_DARK = (94, 155, 84)
PATH = (196, 186, 156)
TREE_DARK = (47, 93, 58)
TREE_MID = (74, 130, 83)
TREE_TRUNK = (107, 79, 58)
WATER = (111, 168, 199)
WATER_HI = (168, 210, 230)
WALL = (200, 196, 184)
ROOF = (182, 91, 74)
DOOR = (107, 91, 74)
PROP = (154, 139, 112)

# ── 室内 ──
INDOOR_BG = (58, 58, 62)
INDOOR_FLOOR = (74, 71, 64)
INDOOR_LINE = (44, 44, 48)

# ── 角色 ──
NPC_SKIN = (236, 206, 178)
NPC_HAIR = (38, 36, 40)
NPC_A_BODY = (58, 63, 69)      # 岚：深灰夹克
NPC_B_BODY = (232, 230, 222)   # 澪：白衬衫
PLAYER_BODY = (120, 168, 122)
PLAYER_HAIR = (52, 44, 38)
SHADOW = (86, 128, 76)        # 草地阴影色（不用 RGBA，避免在非 alpha 画布上画成黑块）

# ── UI ──
BG_TOP = (22, 26, 33)
BG_BOTTOM = (12, 14, 18)
PANEL_BG = (20, 24, 30)
PANEL_LINE = (58, 63, 72)
TEXT = (226, 226, 222)
TEXT_DIM = (150, 152, 158)
TEXT_FAINT = (104, 108, 116)
ACCENT = (214, 158, 96)
ACCENT_DIM = (120, 88, 56)
NEON = (232, 160, 200)
DANGER = (196, 104, 96)
PLAYER_TEXT = (168, 200, 170)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


class Fonts:
    def __init__(self) -> None:
        self._path = self._find()
        self._cache: dict[tuple[int, bool], pygame.font.Font] = {}

    @staticmethod
    def _find() -> str | None:
        for p in _FONT_CANDIDATES:
            if Path(p).exists():
                return p
        match = pygame.font.match_font("microsoftyahei,simhei,notosanscjk,pingfang")
        if match:
            return match
        print("[警告] 没有找到中文字体，界面里的汉字可能显示为方块。", file=sys.stderr)
        return None

    def get(self, size: int, *, bold: bool = False) -> pygame.font.Font:
        key = (size, bold)
        if key not in self._cache:
            font = pygame.font.Font(self._path, size)
            font.set_bold(bold)
            self._cache[key] = font
        return self._cache[key]
