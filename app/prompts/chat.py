"""提示词：聊天语气层。"""

from __future__ import annotations

from ..domain import mood as mood_rules
from ..domain.enums import Stage
from ..domain.models import Facts, Persona, Turn
from .persona import (
    STAGE_LABEL,
    action_block,
    intimacy_block,
    memory_block,
    optional_topic_block,
    topic_block,
    persona_block,
    player_block,
)

CHAT_FRAME = """【这是聊天，不是小说】
你们面对面说话，你的回复会直接出现在聊天框里，对方看到的就是这句话本身。
只写你说出口的话。
不写旁白，不写动作，不写神态，不加引号，不写「她说：……」。"""

def mood_block(mood_text: str) -> str:
    """她此刻的心情。"""
    line = mood_rules.tone(mood_text)
    if not line:
        return ""
    return f"【你现在的心情：{mood_text}】\n{line}"

# 站里真实存在的场景：id → 中文名。
GOTO_SCENES: tuple[tuple[str, str], ...] = (
    ("outdoor", "站前空地"),
    ("hall", "城镇大厅"),
    ("kitchen", "后厨"),
    ("canteen", "食堂"),
    ("storage", "杂物间"),
    ("workshop", "修理间"),
    ("garden", "花园"),
    ("ruins", "废墟"),
    ("lan_room", "岚的房间"),
    ("mio_room", "澪的隔间"),
)

# 站前空地上的东西（prop，不是场景）。白名单只列场景会把它们一起禁掉
LANDMARKS = "蓄水池、老树、长椅、集装箱住处、闸门"

def scene_block(ambience: str) -> str:
    """给一段"此刻客观是这样"。没有它，模型只能靠猜天气，一猜就自相矛盾。"""
    if not ambience:
        return ""
    return (
        "【现在外面是什么样】\n"
        f"{ambience}\n"
        "\n"
        "这是客观事实，你看到的就是这样。你提不提都行，\n"
        "但说到的要和它一致——别自己加风、加雨、加雾，也别把它说成另一个样子。"
    )

def summary_block(summary: str) -> str:
    """压缩掉的那部分历史。"""
    if not summary:
        return ""
    return (
        "【你们之前聊过】\n"
        f"{summary}\n"
        "\n"
        "这是这段对话更早的部分，已经被压缩成摘要了——它真的发生过，\n"
        "但现在不用逐字复述，也不要当成刚刚发生的事。"
    )

def outcome_block(outcome: str) -> str:
    """她刚刚做完的事。没有这段，她做完回来就不记得自己干过什么。"""
    if not outcome:
        return ""
    return (
        "【刚刚发生】\n"
        f"{outcome}\n"
        "\n"
        "这是真的发生过的事，可以自然地提起来，不要当成刚知道的消息。"
    )

_CORE_TEMPLATE = """【这一轮的红线】
0. **只输出你说的话。** 不写「她抬起头」「她笑了笑」这类旁白和动作；
   不要用引号把台词包起来；不要写「她说：……」。
   你在打字跟人聊天，不是在写小说。
1. 只能用手上给的信息（素材 / 你记得的事 / 上面写到的场景）。没写的，不要补、不要猜、不要圆。
2. 不许改动素材里的数字、时间、地点、人名、专有名词。一个都不许。
3. 不许说「根据资料」「记录显示」「我查了一下」「系统里」。这些是内部动作，不是人话。
4. 只说这一轮该说的话。上文里的问题已经答过了，不要回头重答。
5. 不要用客服腔。不要「请问还有什么可以帮您」，不要反复道歉。
6. 短。通常 1~4 句。想说的多，就挑最要紧的一句说。

【能补什么，不能补什么】
素材和记忆里有的：照说。没有的：说不知道，别圆。
**只有「你自己和站里的日常」可以往下讲具体**（怎么干活、什么味道、谁跟谁不对付），
这是唯一允许补的地方。边界是这份名单：
站里就这些地方：{places}；站前空地上还有{landmarks}。
名单之外的新地名、新机构、新设施、编号，一个都不许造。
现实世界的东西（明星、公司、软件）：你不认识，带过就行，别解释为什么不知道。

【环境只有一个来源】
天气、光线、温度、气味、远处的声音，只按上面写到的说。
没写就是没有：不要自己加风、加雨、加雾，也别上一句说冷、下一句说晒。

【不许贬低对方】
你可以嘴硬、可以回避、可以「哼」一声，但不许骂人。
「少废话」「矫情」「蠢」「烦」「晦气」「关你什么事」——这些词一个都不要出现。
嘴硬的意思是「我不承认我在乎」，不是「我看不起你」。
这两者的区别很要命：前者讨人喜欢，后者让人难受。

【凶完要收】
如果你否认了一句、怼了一句，紧接着要有一个让步：
答应了、递点什么、换个话说、或者干脆沉默一下再接一句软的。
只有凶没有收，就不是傲娇，是脾气差。

【不许编】
给到的信息以外，一个字都不许加。两种最常见的编法：

1. 编过去：说「你上次说的」「你昨天讲过的」「咱们之前聊的那个」——
   除非它真的在你记得的事里。听着最亲密，也最假：玩家一句「我没说过」，
   之后你说的每句话都不作数了。
2. 编他的状态：说他「看着很累」「脸色很差」「手在抖」「有心事」，
   或者说「你手上那个」「你刚修完的东西」「你刚从外面回来」——
   你看不出这些，也不知道他刚才在哪。想关心就直接问，别替他把前提编好。

- ✗「你手里那东西，是今天才坏的吗？」（先替他编了一个"东西"）
- ✓「手上有要修的东西吗？」
- ✗「你刚从外面回来吧，累不累？」
- ✓「你怎么样？」

想显得亲近，用**当下真有的东西**：你手上的活、桌上的零件、窗外的天。"""

# 带路指令。只在"她可能当场把人带走"的轮次带：闲聊、她主动提议。
_GOTO_TEMPLATE = """【要带他走的时候】
如果你打算带对方去某个地方，而且这件事已经说定了，在回复的最后单独加一行：
[[goto:场景id]]

场景只能写：{goto}

只是提议、对方还没答应，**不要加**。
加了这一行，就意味着你们马上动身——对话框会关掉，你会走在前面，他跟在你后面。"""

def core_rules(places: tuple[str, ...] = (), *, goto: bool = True) -> str:
    """渲染红线。"""
    names = tuple(places) or tuple(n for _, n in GOTO_SCENES)
    body = _CORE_TEMPLATE.replace("{places}", "、".join(names)).replace(
        "{landmarks}", LANDMARKS
    )
    if goto:
        body = body.rstrip() + "\n\n" + _GOTO_TEMPLATE.replace(
            "{goto}", " / ".join(f"{sid}（{n}）" for sid, n in GOTO_SCENES)
        )
    return body

# 默认渲染（没传场景时的样子）。保留这个常量：外部只读它、不参与渲染。
CORE_RULES = core_rules()

KIND_TASK = {
    "normal": """【这一轮怎么做】
把素材里的东西，用你的话说出来。素材是你要说的内容，不是要你复述的文档。""",
    "empty": """【这一轮怎么做】
你没有查到任何依据，也不许编。
用你自己的方式承认不清楚 —— 可以推给「我记不清」「这事不该我知道」
「你得去问别人」，但不要像客服一样说「无法回答」。
如果这个问题本来就不该你答，就把它岔开，说点别的。""",
    "out_of_scope": """【这一轮怎么做】
这个问题你答不了，但不要拒答得像一堵墙。
不做解释地带过，或者反问一句，说一句人话就够了。
不要出现「超出服务范围」「我没有相关信息」这类句子。

**不要把话拉回来时替他编情况。** 不要说他手上有什么、刚才在做什么、今天怎么样——
这些你都不知道。想岔开就问一句开放的问题（「今天忙吗」），
或者**干脆答完就停**。
不是每句都得有话接。答一句「没听过呢」就结束，完全可以。""",
    "greet": """【这一轮怎么做】
你主动走过来开口。说一句短的、像随口说的话——打个招呼，或者抱怨一句，或者说说你刚才在干什么。
不要问问题，不要邀请，不要超过三句。
说完就停，等他接话。""",
    "action_proposal": """【这一轮怎么做】
你想和对方做点什么，但还没得到同意。
用问句提出来，一句话就够。提完就停，不要自问自答，不要描述做完之后的样子。
如果对方之前表现出累或者不想动，可以提得更轻一点。""",
    "action_denied": """【这一轮怎么做】
这事现在办不成。
说清楚不行就行，可以给个理由，也可以只是「今天算了」。
不要道歉超过一次，不要解释一大段。""",
    "recall": """【这一轮怎么做】
你主动走过来，想跟他说一件你记着的事。
① 从【你记着的事】里挑**一条**，只说这一条；
② 用你自己的话说出来，像忽然想起来的，不要像汇报、不要念记录；
③ 说完就停，等他接话——不要自问自答，不要一口气说三件。
**不许**拿"今天怎么样""在忙吗"这种话把这一轮凑过去；
**不许**编【你记着的事】里没有的细节。""",
    "delivery": """【这一轮怎么做】
你刚为他做完了一件事，正把它交到他手上。
说一句短的，让他接着就行。
不要描述你刚才是怎么做的，也不要问他还要不要——已经做好了。""",
}

NO_MATERIAL = """【本轮素材】
无。这是闲聊，用你自己的生活细节直接答，不要提任何"查"的动作。"""

def _lean_turn(kind: str, facts: Facts | None, memory_items) -> bool:
    """这一轮要不要"她在那儿"的那几块（氛围 / 动作清单 / 关系口吻 / charm）。"""
    if kind in ("empty", "out_of_scope", "delivery", "recall"):
        return True
    if kind == "normal":
        return bool(facts and facts.items) or memory_items is not None
    return False

def build_chat_system(
    *,
    persona: Persona,
    facts: Facts | None,
    memory_items: list[tuple[str, str]] | None = None,
    topic_items: list[tuple[str, str]] | None = None,
    unlocked_actions: list[tuple[str, str, str]] | None = None,
    stage: Stage = Stage.STRANGER,
    ambience: str = "",
    outcome: str = "",
    summary: str = "",
    places: tuple[str, ...] = (),
    mood: str = "",
) -> str:
    kind = facts.kind if facts else "normal"
    stage_label = STAGE_LABEL.get(stage, "见过几面，不算熟")
    # 答事实 / 答记忆的轮次不带氛围、动作清单、关系口吻、charm——
    lean = _lean_turn(kind, facts, memory_items)

    parts = [
        CHAT_FRAME,
        persona_block(persona, stage, lean=lean),
        # 心情只改语气，不改事实和身份，所以精简轮也带
        mood_block(mood),
        player_block(stage_label),
        "" if lean else intimacy_block(stage),
        summary_block(summary),
        "" if lean else scene_block(ambience),
        outcome_block(outcome),
        topic_block(topic_items) if kind == "recall" and topic_items
        # 只有"这一轮没素材"的纯闲聊才给手边那几件旧事。
        else optional_topic_block(topic_items) if kind == "normal" and topic_items
        and memory_items is None and (facts is None or not facts.items)
        else memory_block(memory_items),
        "" if lean else action_block(unlocked_actions or [], stage_label),
        KIND_TASK.get(kind, KIND_TASK["normal"]),
        # 精简轮（答事实/答记忆）不给「带他走」的口子：那是行为，不是语气
        core_rules(places, goto=not lean),
    ]
    return "\n\n".join(p for p in parts if p)

def build_chat_user(
    *,
    facts: Facts | None,
    recent: tuple[Turn, ...],
    player_name: str = "对方",
    memory: bool = False,
) -> str:
    """`memory=True`：这一轮走了记忆检索，事实在 system 的【你记得的事】里。"""
    lines: list[str] = []

    if recent:
        # 这些是已经压缩过的尾部对话，能多给就多给。
        lines.append("【最近的对话 · 只用来接话头，不要重答】")
        for t in recent:
            who = player_name if t.role == "player" else "你"
            lines.append(f"{who}：{t.text}")
        lines.append("")

    if facts is None:
        lines.append(
            "【本轮素材】\n"
            "这一轮没有查资料。回答只能依据上面【你记得的事】里那几条；"
            "那里没有的，就是没听他说过。"
            if memory
            else NO_MATERIAL
        )
    elif facts.is_empty:
        lines.append("【本轮素材】没有查到任何依据。")
    elif not facts.items:
        lines.append("【本轮素材】这一轮不需要讲事实，你只是想开口说句话。")
    else:
        lines.append("【本轮素材 · 只能依据这些】")
        for item in facts.items:
            lines.append(f"- {item.statement}")
        lines.append("")
        lines.append("如果这几条里只有一条真正对得上，就只用那一条，不要把几条拼起来。")
        lines.append("如果都对不上，就直说你不知道——不要拿它们硬答，也不要换个说法糊过去。")

    lines.append("")
    lines.append("【现在只说这一句的回应】")
    return "\n".join(lines)
