"""把提示词用真实数据渲染到 PROMPTS_PREVIEW.md，不连模型。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.decision.planner import INTENT_HINT  # noqa: E402
from app.domain.enums import Stage  # noqa: E402
from app.domain.models import Facts, Turn  # noqa: E402
from app.persona.builtin import PERSONAS  # noqa: E402
from app.prompts.behavior import build_decision_system  # noqa: E402
from app.prompts.chat import build_chat_system, build_chat_user  # noqa: E402
from app.prompts.memory import build_extract_system, build_extract_user  # noqa: E402
from app.prompts.persona import intimacy_block, persona_block  # noqa: E402

LAN = PERSONAS["A"]

MEMORY_ITEMS = [
    ("关于对方", "失忆：记不清云塌之前的事"),
    ("你答应过的事", "答应带他去看闸门"),
    ("你知道的事", "怕黑：晚上灯从来不关"),
]

UNLOCKED = [
    ("去灶上端点热的回来", "他脸色不好、或者坐了半天没说话", "serve"),
    ("让你坐下，把你的手翻过来看", "他身上有伤、或者刚从外面回来", "serve"),
    ("一起走走", "两个人都闲下来的时候", "invite"),
    ("去她那儿坐坐", "关系够近、她主动的时候", "invite"),
]

TS = datetime(2026, 10, 20, 14, 20, 0)

RECENT_TURNS = (
    Turn(idx=0, role="player", text="那些楼你闭着眼都能走，是真的？", ts=TS),
    Turn(idx=1, role="npc", text="……差不多吧。走多了就记住了。", ts=TS),
    Turn(idx=2, role="player", text="那你不怕走错吗", ts=TS),
    Turn(idx=3, role="npc", text="怕有什么用。", ts=TS),
)

FACTS = Facts.of(
    [
        "闸门每天 6:00 开、18:00 关，进出要在本子上签名",
        "站外的磁悬浮轨道一直伸进城里，路上有残响",
    ],
    kind="normal",
)

TRANSCRIPT = """玩家：那些楼你闭着眼都能走，是真的？
岚：……差不多吧。走多了就记住了。
玩家：那你不怕走错吗
岚：怕有什么用。
玩家：明天我要出闸一趟
岚：去哪儿。
玩家：北边那片楼。听说那边还有电。
岚：……一个人去？
玩家：嗯。
岚：你连路都不认识。我跟你去。
玩家：那闸门那边说得通吗
岚：我说得通就行。六点，闸门口。别迟到。"""

ALLOWED_A = ["idle", "rest", "seek_player", "share_memory", "solo", "withdraw", "work"]
ALLOWED_B = ["work", "solo", "share_memory", "seek_player"]


def hints(names: list[str]) -> list[str]:
    return [f"{n} —— {INTENT_HINT.get(n, '')}" for n in names]


BATCH_ROW_B = {
    "npc_id": "B",
    "你是谁": PERSONAS["B"].identity,
    "你们的关系": "熟人",
    "现在": {
        "时间": "14:20",
        "她在": "修理间",
        "玩家在": "修理间",
        "体力": 0.93,
        "情绪": "calm",
    },
    "允许的意图": hints(ALLOWED_B),
    "待办约定": [],
    "最近做过的事": [],
}

DECISION_PAYLOAD = {
    "你是谁": LAN.identity,
    "你们的关系": "亲近",
    "现在": {
        "时间": "14:20",
        "她在": "站前空地",
        "玩家在": "修理间",
        "体力": 0.71,
        "情绪": "calm",
    },
    "允许的意图": hints(ALLOWED_A),
    "待办约定": ["答应带他去看闸门"],
    "最近做过的事": ["solo", "rest"],
}


def block(title: str, lang: str, text: str) -> str:
    return f"### {title}\n\n```{lang}\n{text}\n```\n"


def main() -> None:
    chat_system = build_chat_system(
        persona=LAN,
        facts=FACTS,
        memory_items=MEMORY_ITEMS,
        unlocked_actions=UNLOCKED,
        stage=Stage.CLOSE,
        ambience="天是晴的，风不大。太阳有点晃眼，空气里有铁锈和草晒过的味道。",
    )
    chat_user = build_chat_user(facts=FACTS, recent=RECENT_TURNS)

    parts: list[str] = []
    parts.append("# 三类提示词实录\n")
    parts.append(
        "由 `tools/show_prompts.py` 用游戏内的真实人设/场景/记忆渲染，"
        "不连模型。改了模板重跑即可核对。\n"
    )

    parts.append("## 一、决策（NPC 自主行为）\n")
    parts.append(
        "触发时机：每 20 秒，或 F2 推进一小时。输出只有一个意图词。\n"
    )
    parts.append(block("system", "text", build_decision_system()))
    parts.append(block("user（planner 组装的 payload）", "json",
                       json.dumps(DECISION_PAYLOAD, ensure_ascii=False, indent=2)))

    parts.append("\n---\n\n## 一·附：同一时刻的第二个角色\n")
    parts.append(
        "轮到她时是**另一条完全独立的请求**，只是并发发出去。"
        "system 用的是同一段规则模板，人设和处境全在各自的 payload 里。\n"
    )
    parts.append(
        block(
            "user（澪的 payload）",
            "json",
            json.dumps(BATCH_ROW_B, ensure_ascii=False, indent=2),
        )
    )

    parts.append("\n---\n\n## 二、聊天\n")
    parts.append(
        "触发时机：玩家每发一句话。system 由人设块 + 记忆块 + 动作清单 + 本轮任务 + 全局规则拼成。\n"
    )
    parts.append(block("system", "text", chat_system))
    parts.append(block("user", "text", chat_user))

    mio_block = persona_block(PERSONAS["B"])
    mio_start = mio_block.find("【你怎么说话】")
    mio_end = mio_block.find("【你容易在哪儿出错")
    parts.append("\n---\n\n## 二·附：澪的说话部分（对照口吻）\n")
    parts.append(
        "同一套渲染逻辑，换了张人设卡。上面是傲娇，这里是温柔知心大姐姐。\n"
    )
    parts.append(block("system 片段", "text", mio_block[mio_start:mio_end].rstrip()))

    parts.append("\n---\n\n## 二·附二：关系决定口吻\n")
    parts.append(
        "傲娇不是基础人格，是关系到位后才解锁的。下面是同一句「你在看什么呢」"
        "在三个阶段的完整回答规则和示范。\n"
    )
    for st, title in (
        (Stage.STRANGER, "陌生"),
        (Stage.CLOSE, "亲近"),
        (Stage.AMBIGUOUS, "暧昧"),
    ):
        parts.append(f"### {title}\n")
        parts.append(block("口吻规则", "text", intimacy_block(st)))
        pb = persona_block(LAN, st)
        demo = pb.find("【说话示范")
        if demo > 0:
            parts.append(block("示范", "text", pb[demo:]))

    parts.append("\n---\n\n## 三、压缩（会话中触发 + 收尾）\n")
    parts.append(
        "触发时机：估算 token 超过 6000 时压前文 40%；会话结束时做 final 抽取。"
        "两者共用同一套规则，只有末尾的 MODE_NOTE 不同。\n"
    )
    parts.append(block("system（compact）", "text", build_extract_system("compact")))
    parts.append(
        block(
            "user",
            "text",
            build_extract_user(TRANSCRIPT, "玩家问起灯的来历，岚敷衍带过。"),
        )
    )

    parts.append("\n---\n\n## 长度统计\n")
    rows = [
        ("决策 system", 0, len(build_decision_system())),
        ("决策 payload", 0, len(json.dumps(DECISION_PAYLOAD, ensure_ascii=False))),
        ("聊天 system", 0, len(chat_system)),
        ("聊天 user", 0, len(chat_user)),
        ("压缩 system", 0, len(build_extract_system("compact"))),
    ]
    parts.append("| 段 | 字符数 | 估算 token（×0.7） |")
    parts.append("|---|---|---|")
    for name, _, chars in rows:
        parts.append(f"| {name} | {chars} | {int(chars * 0.7)} |")

    out = ROOT / "PROMPTS_PREVIEW.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
