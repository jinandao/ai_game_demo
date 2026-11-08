"""语气层，把中性事实说成角色口吻。"""

from __future__ import annotations

import re

from ..domain.enums import Stage
from ..domain.models import Facts, Persona, Turn
from ..infra.llm import LLMClient, LLMError
from ..prompts.chat import GOTO_SCENES, build_chat_system, build_chat_user

_QUOTED = re.compile(r'[「"“]([^」"”]{1,300})[」"”]')
_OUTER = re.compile(r'^[\s「"“]+(.*?)[\s」"”]+$', re.S)
_NARRATION_HINT = re.compile(r"她|他|抬头|低头|笑了|转身|顿了顿|看了看")

# 模型想带玩家走的时候，在回复末尾加这个标记。
_GOTO = re.compile(r"\[\[\s*goto\s*:\s*([a-z_]+)\s*\]\]", re.I)
# 和提示词里的白名单同源（`prompts/chat.py GOTO_SCENES`）。
VALID_SCENES = {sid for sid, _ in GOTO_SCENES}

def _extract_goto(text: str) -> tuple[str, str | None]:
    """剥离 [[goto:xxx]]，返回（干净台词，目标场景）。"""
    match = _GOTO.search(text)
    if match is None:
        return text, None
    scene = match.group(1).lower()
    cleaned = _GOTO.sub("", text).strip()
    return cleaned, scene if scene in VALID_SCENES else None

def _strip_narration(text: str) -> str:
    """把"小说体"扒成纯台词。"""
    stripped = text.strip()
    if not _QUOTED.search(stripped):
        m = _OUTER.match(stripped)
        return m.group(1).strip() if m else stripped

    kept: list[str] = []
    for i, seg in enumerate(_QUOTED.split(stripped)):
        if i % 2 == 1:                    # 引号内 = 台词
            kept.append(seg.strip())
            continue
        for sentence in re.split(r"(?<=[。！？…])", seg):
            s = sentence.strip()
            if s and not _NARRATION_HINT.search(s):
                kept.append(s)

    return "".join(kept).strip()

class Stylist:
    def __init__(self, llm: LLMClient, max_recent: int = 24) -> None:
        """max_recent：给模型多少条历史（一条 = 一次发言）。"""
        self._llm = llm
        self._max_recent = max_recent
        self.last_system_prompt: str = ""   # 调试面板要能看见真正发出去的提示词
        self.last_error: str = ""           # 上一次调用失败的原因，调试面板用
        self.last_goto: str | None = None   # 这一轮她说要带玩家去哪

    def render(
        self,
        facts: Facts | None,
        *,
        persona: Persona,
        recent_turns: tuple[Turn, ...],
        memory_items: list[tuple[str, str]] | None = None,
        topic_items: list[tuple[str, str]] | None = None,
        unlocked_actions: list[tuple[str, str, str]] | None = None,
        stage: Stage = Stage.STRANGER,
        ambience: str = "",
        outcome: str = "",
        summary: str = "",
        places: tuple[str, ...] = (),
        mood: str = "",
        recent_limit: int | None = None,
        on_delta=None,
    ) -> str:
        """recent_limit：这一轮给多少条历史，None = 用默认值。"""
        limit = self._max_recent if recent_limit is None else recent_limit
        system = build_chat_system(
            persona=persona,
            facts=facts,
            memory_items=memory_items,
            # 主动开口时，话题只能从这几条记忆里挑（见 prompts/chat.py 的 recall 分支）
            topic_items=topic_items,
            unlocked_actions=unlocked_actions,
            stage=stage,
            ambience=ambience,
            outcome=outcome,
            summary=summary,
            places=places,
            mood=mood,
        )
        user = build_chat_user(
            facts=facts,
            recent=recent_turns[-limit:] if limit > 0 else recent_turns,
            # 走了记忆检索的轮次，user 里不能再写「这是闲聊」
            memory=bool(memory_items),
        )
        self.last_system_prompt = system
        self.last_goto = None

        try:
            text = self._llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.85,
                max_tokens=300,
                on_delta=on_delta,
            )
            self.last_error = ""
        except LLMError as e:
            self.last_error = str(e)[:160]
            return "……"

        text, self.last_goto = _extract_goto(text.strip())
        return self._guard(text, persona)

    @staticmethod
    def _guard(text: str, persona: Persona) -> str:
        text = _strip_narration(text)
        for word in persona.has_banned(text):
            text = text.replace(word, "")
        return text.strip() or "……"
