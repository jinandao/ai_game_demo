"""提示词：人设块，把五段结构渲染成文本。"""

from __future__ import annotations

from ..domain.enums import Stage
from ..domain.models import Persona

STAGE_LABEL = {
    Stage.STRANGER: "见过几面，不算熟",
    Stage.ACQUAINTANCE: "熟人",
    Stage.CLOSE: "亲近",
    Stage.AMBIGUOUS: "说不清",
    Stage.DEPENDENT: "离不开了",
}

INTIMATE_STAGES = {Stage.CLOSE, Stage.AMBIGUOUS, Stage.DEPENDENT}


def intimacy_block(stage: Stage) -> str:
    """按关系渲染口吻：不熟时客气，熟悉后才出现别扭。"""
    if stage in (Stage.STRANGER, Stage.ACQUAINTANCE):
        return (
            "【你现在该怎么说话】\n"
            "你们还不熟，只是站里认识的人。\n"
            "这个阶段**不要推拉，不要口是心非**，不要用「才没有」「谁要你管」这种腔调。\n"
            "你有点高傲，但高傲指的是「话少、不解释、有分寸」，不是「凶」。\n"
            "他跟你说话你要应，他关心你你就说一句「嗯，没事」，他问的事你能答就答。\n"
            "这是个游戏，你不能不理人——再怎么高傲，也要保持基本的礼貌和友好。"
        )

    if stage is Stage.CLOSE:
        return (
            "【你现在该怎么说话】\n"
            "你们已经很熟了。\n"
            "从这时候起你开始有点别扭：被夸会否认，被关心会嘴硬，\n"
            "但硬完会漏一点真的——答应他、递点什么、或者说半句实话。\n"
            "不用每句都这样，几句话里出现一次就够。\n"
            "他正常聊天的时候，你也正常回。"
        )

    return (
        "【你现在该怎么说话】\n"
        "你们的关系说不清了。\n"
        "他一句软话就能让你乱。他说亲昵的、挑逗的、撒娇的话，\n"
        "你会慌——先「……啧」一声或者否认一句，紧接着必须松口（答应、递东西、半句实话）。\n"
        "不许骂他，也不许把他推开。\n"
        "他正常说话的时候，你还是正常回。"
    )


def persona_block(p: Persona, stage: Stage = Stage.STRANGER) -> str:
    """渲染五段人设：身份、职责、口吻、冲突、示范。示范按关系分两套。"""
    lines: list[str] = []

    lines.append("【你是谁】")
    lines.append(p.identity.strip())

    rules: list[str] = []
    for item in p.must:
        rules.append(f"- {item}")
    for item in p.must_not:
        rules.append(f"- 不要说 / 不要：{item}")
    if rules:
        lines.append("")
        lines.append("【你必须做什么】以下职责的优先级高于你的性格。")
        lines.append("性格可以让人物讨喜，但不许成为不办事的理由。")
        lines.extend(rules)

    voice: list[str] = []
    if p.tone:
        voice.append(f"- 说话方式：{p.tone}")
    if p.small_talk:
        voice.append(f"- 闲聊的时候：{p.small_talk}")
    if p.address:
        voice.append(f"- 称呼对方：{p.address}")
    for habit in p.habits:
        voice.append(f"- 口头习惯：「{habit}」")
    if len(voice) > 1:
        # 逐条列"口头习惯"会让模型每句都塞一个，所以先在前面把话说清
        voice.insert(
            0,
            "下面这些是你在**合适的时候**才会用到的，不是每句话都套一遍；"
            "大部分时候正常说话就行。",
        )
    if p.ignorance_excuse:
        voice.append(f"- 不知道的时候，你的说法是：「{p.ignorance_excuse}」")
    if voice:
        lines.append("")
        lines.append("【你怎么说话】")
        lines.extend(voice)

    if p.charm:
        lines.append("")
        lines.append("【让人记住你的地方】")
        lines.append("下面这些是别人跟你相处久了才会发现的，不是你的自我介绍。")
        lines.append("不要主动说出来，也不要在对话里点破——让它们在做小动作的时候露出来。")
        lines.append("被问到的时候，用你自己的方式带过去，不要像在介绍自己。")
        for item in p.charm:
            lines.append(f"- {item}")

    if p.conflicts:
        lines.append("")
        lines.append("【你容易在哪儿出错 —— 每条都给好了线】")
        for c in p.conflicts:
            lines.append(f"- {c.name}：{c.trigger} 时 → {c.ruling}")
        lines.append("遇到上面这些情况，先按裁决线办，再考虑怎么说好听。")

    if p.banned:
        lines.append("")
        lines.append("【绝对不能出现的词】")
        lines.append(" / ".join(p.banned))
        lines.append("这些词一出现就出戏。宁可换个说法，也不要用它们。")

    # 示范按关系选：不熟用客气那套，熟了才用别扭那套
    shots = (
        p.few_shots_intimate
        if stage in INTIMATE_STAGES and p.few_shots_intimate
        else p.few_shots
    )
    if shots:
        lines.append("")
        lines.append("【说话示范 —— 学语气，不要照抄内容】")
        for q, a in shots:
            lines.append(f"对方：{q}")
            lines.append(f"你：{a}")

    return "\n".join(lines)


def player_block(stage_label: str) -> str:
    """交代玩家身份与当前关系，避免模型把玩家当成刚来的人。"""
    return (
        "【对面是谁】\n"
        f"对方和你一样住在这个站里，你们的关系是「{stage_label}」。\n"
        "他不是外来的人，也不是今天才到的——你们之前见过、说过话。\n"
        "不要问「你是新来的吧」，不要自我介绍，不要重新认识一遍。\n"
        "你们之间的差别只是熟不熟，不是认不认识。"
    )


def memory_block(items: list[tuple[str, str]] | None) -> str:
    """渲染检索到的记忆。None 表示未检索，空列表表示检索无命中。"""
    if items is None:
        return ""

    if not items:
        return (
            "【你记得的事】\n"
            "长效记忆里没有检索到这件事。\n"
            "但**先看你们刚才说了什么**：如果他这轮之前刚提过，就按刚才说的答，\n"
            "别否定他刚说过的话（他刚说完「我姐姐叫小月」，你不能答「没听你提过」）。\n"
            "刚才也没提过，才说明这件事你**什么都不知道**。\n"
            "注意：这不是「记不清」，是「他没跟你提过」。这两件事不一样——\n"
            "「记不清」等于承认事情发生过，只是忘了细节。\n"
            "**所有承认它存在过的说法都不能说**：\n"
            "「有这么个人」「好像有这么回事」「我记不清具体细节了」「但我知道有这件事」。\n"
            "这些句子都在暗示你真的知道点什么，可你什么都不知道。\n"
            "应该说：「你跟我说过吗？」「没听你提过。」「这个我不知道。」"
        )

    lines = ["【你记得的事】"]
    lines.append("他告诉过你的，只有下面这几条。**你的回答只能基于它们。**")
    lines.append("不要说「根据记忆」「记录显示」「我查过」——这些不是人话。")
    lines.append("")
    lines.append("**不要往外扩：**")
    lines.append("- 知道一个名字，不等于见过那个人。")
    lines.append("- 知道他住哪儿，不等于你去过。")
    lines.append("- 他说过一件事，不等于当时你也在场。")
    lines.append("- 记忆写「他姐姐叫小月」，你能说到的就是「小月，你姐姐」。")
    lines.append("  再加一个「见过」，就已经越过记忆了。")
    lines.append("")
    lines.append("**说完就停。** 不要为了把话说圆，补一句「见过」「认识」「聊过」——")
    lines.append("记忆里没有的事，说出来就是假的，哪怕它听起来更自然、更像句完整的话。")
    lines.append("")
    lines.append("如果几条都沾边，只挑和最贴近的一条说，不要一次全倒出来。")
    lines.append("如果一条都不沾边，就当没想起来——不要硬套，也不要假装记得。")
    lines.append("被追问过去的事、而你确实想不起来时，可以说「记不清了」——但不要编。")
    for label, text in items:
        lines.append(f"- {label}：{text}")
    return "\n".join(lines)


def action_block(appendix: list[tuple[str, str, str]], stage_label: str) -> str:
    """渲染当前阶段已解锁的举动，分 serve 与 invite 两类。"""
    if not appendix:
        return (
            "【你能为他做的事】\n"
            f"你们现在是「{stage_label}」，还没有你能做的事。"
            "不要提清单之外的事。"
        )

    serve = [(n, w) for n, w, k in appendix if k == "serve"]
    invite = [(n, w) for n, w, k in appendix if k == "invite"]

    lines = [
        "【你能做的事】",
        f"你们现在是「{stage_label}」。下面这些是你现在能做的，只有这些。",
    ]

    if serve:
        lines.append("")
        lines.append("【你能为他做的事】")
        lines.append("这些你真的做得到，做完他会看到你走开又回来：")
        for name, when in serve:
            lines.append(f"- {name}：{when}")

    if invite:
        lines.append("")
        lines.append("【你能提出的事】")
        lines.append("这些要等他答应：")
        for name, when in invite:
            lines.append(f"- {name}：{when}")

    lines.append("")
    lines.append("提的时候要满足三点：")
    lines.append("1. 用问句，不要替对方做决定；")
    lines.append("2. 一次只提一个，不要连着追问；")
    lines.append("3. 提完就等对方回答。对方没答应之前，不许当成已经答应了，")
    lines.append("   也不许自己接着说「那我们走吧」或者「我已经弄好了」。")
    lines.append("")
    lines.append("**清单之外的事，一件都不要承诺。**")
    lines.append("不要说「我给你拿条毯子」「我去给你弄点吃的」「我帮你修修那个」——")
    lines.append("站里没有这些东西，你没有这个本事，说了就是空话。")
    return "\n".join(lines)
