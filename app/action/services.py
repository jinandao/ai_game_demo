"""照料行为：她为玩家做的事。

统一流程：她提议 → 玩家确认 → 她去做 → 交付。按好感阶段逐层解锁。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import Stage

_ORDER = [
    Stage.STRANGER,
    Stage.ACQUAINTANCE,
    Stage.CLOSE,
    Stage.AMBIGUOUS,
    Stage.DEPENDENT,
]


@dataclass(frozen=True)
class ServiceAction:
    id: str
    label: str          # 给模型和按钮看：「去灶上端点热的回来」
    npc_id: str
    stage: Stage        # 最低好感度
    scene: str          # 去哪儿做；"" = 原地演
    prop: str           # 走到哪个道具旁；"" = 场景入口
    outcome: str        # 做完之后写进对话的一句话
    when: str           # 她什么时候会想到做这个


SERVICES: tuple[ServiceAction, ...] = (
    # ── 澪：她关心人的方式是动手 ──
    ServiceAction(
        id="hand_drink",
        label="去灶上端点热的回来",
        npc_id="B",
        stage=Stage.STRANGER,
        scene="canteen",
        prop="stove",
        outcome="她刚从灶上端了一杯热的回来，递到你手上。茶甜得发腻。",
        when="他脸色不好，或者坐了半天没说话",
    ),
    ServiceAction(
        id="point_seat",
        label="指指旁边的凳子，让你坐下",
        npc_id="B",
        stage=Stage.STRANGER,
        scene="",
        prop="",
        outcome="她抬下巴指了指旁边的凳子，没抬头，手上还在弄东西。",
        when="他站着不动、或者一直在旁边看她干活",
    ),
    ServiceAction(
        id="check_wound",
        label="让你坐下，把你的手翻过来看",
        npc_id="B",
        stage=Stage.ACQUAINTANCE,
        scene="workshop",
        prop="workbench",
        outcome="她让你坐下，把你的手翻过来看了会儿，说没伤到骨头。",
        when="他身上有伤、或者刚从外面回来",
    ),
    ServiceAction(
        id="fetch_cloth",
        label="去工作台拿块布，回来擦你手上的脏",
        npc_id="B",
        stage=Stage.ACQUAINTANCE,
        scene="workshop",
        prop="workbench",
        outcome="她从工作台拿了块布回来，抓过你的手把脏擦掉，力气不小。",
        when="他手上脏、或者刚碰过什么不干净的东西",
    ),
    ServiceAction(
        id="give_pill",
        label="从零件柜那边翻点药给你",
        npc_id="B",
        stage=Stage.ACQUAINTANCE,
        scene="workshop",
        prop="shelf",
        outcome="她从零件柜那边翻出一板止痛片，塞到你手里。",
        when="他说哪里疼、或者要出远门",
    ),
    ServiceAction(
        id="tune_arm",
        label="让你抬起胳膊，接上接口调一会儿",
        npc_id="B",
        stage=Stage.CLOSE,
        scene="workshop",
        prop="workbench",
        outcome="她让你抬起胳膊，接上接口调了一会儿，说好了。",
        when="他动作不顺手、或者提过义体有毛病",
    ),
    ServiceAction(
        id="keep_meal",
        label="从灶上端一碗给你留着的东西",
        npc_id="B",
        stage=Stage.CLOSE,
        scene="canteen",
        prop="stove",
        outcome="她端了一碗东西回来，说是给你留着的。",
        when="饭点过了、或者他一天没吃东西",
    ),
    ServiceAction(
        id="give_bandage",
        label="从架子上拿一卷绷带塞进你包里",
        npc_id="B",
        stage=Stage.CLOSE,
        scene="workshop",
        prop="shelf",
        outcome="她从架子上拿了卷绷带，塞进你包里。「自己缠，别包太紧。」",
        when="他要出闸、或者身上有伤没处理",
    ),
    ServiceAction(
        id="lean_check",
        label="凑得很近看看你的脸",
        npc_id="B",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她凑得很近看了你一会儿，手指在你脸侧停了一下。",
        when="他情绪不对、或者你们刚安静下来",
    ),
    ServiceAction(
        id="brush_hair",
        label="把你额前的头发拨开",
        npc_id="B",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她伸手把你额前的头发拨开，看了看，又慢慢放下手。",
        when="他刚回来、头发乱着，或者在低头不看她",
    ),
    ServiceAction(
        id="hold_hand",
        label="握住你的手，不松开",
        npc_id="B",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她把你的手握住，没说话，也没松开。",
        when="他说了不好受的话、或者要走的时候",
    ),
    ServiceAction(
        id="give_key",
        label="把里间的钥匙给你",
        npc_id="B",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她把一把很小的钥匙放到你手心里。「灯我给你留着。」",
        when="天晚了、或者他说不想回去",
    ),
    # ── 岚：她关心人的方式是笨的 ──
    # 送东西在废墟里是大事。
    ServiceAction(
        id="toss_ration",
        label="从包里摸出一包压缩口粮扔给你",
        npc_id="A",
        stage=Stage.STRANGER,
        scene="",
        prop="",
        outcome="她从包里摸出一包压缩口粮扔过来，没看你。「别指望第二包。」",
        when="他一天没吃东西、或者马上要出闸",
    ),
    ServiceAction(
        id="throw_jacket",
        label="把外套脱下来扔到你身上",
        npc_id="A",
        stage=Stage.CLOSE,
        scene="",
        prop="",
        outcome="她把外套脱下来扔到你身上，自己抱着胳膊说「别指望我第二件」。",
        when="天黑风大、他穿得少",
    ),
    ServiceAction(
        id="give_card",
        label="从名片里抽一张给你",
        npc_id="A",
        stage=Stage.CLOSE,
        scene="",
        prop="",
        outcome="她从口袋里抽出一张折成三角的名片，塞进你手里。上面有她用铅笔画的一道线。",
        when="他要一个人出闸、或者问他路怎么走",
    ),
    ServiceAction(
        id="check_gear",
        label="把你的包翻一遍，重新扣好带子",
        npc_id="A",
        stage=Stage.ACQUAINTANCE,
        scene="",
        prop="",
        outcome="她把你的包翻了一遍，把带子重新扣好，没解释。",
        when="他要出闸、或者刚从外面回来",
    ),
    ServiceAction(
        id="shove_shoulder",
        label="撞一下你的肩膀",
        npc_id="A",
        stage=Stage.ACQUAINTANCE,
        scene="",
        prop="",
        outcome="她走过来撞了你肩膀一下，说「走了」，语气像是在催，其实在等你跟上。",
        when="气氛太闷、他说要走或者不说话",
    ),
    ServiceAction(
        id="block_front",
        label="往前一步，把你挡在身后",
        npc_id="A",
        stage=Stage.ACQUAINTANCE,
        scene="",
        prop="",
        outcome="她往前跨了一步，把你挡在身后，嘴上还在骂。",
        when="外面有动静、或者有人朝他走过来",
    ),
    ServiceAction(
        id="show_road",
        label="带你到闸门口，指给你看",
        npc_id="A",
        stage=Stage.CLOSE,
        scene="outdoor",
        prop="gate",
        outcome="她带你到闸门口，抬下巴指了指北边那片楼。",
        when="他问起外面、或者提过要出闸",
    ),
    ServiceAction(
        id="grab_wrist",
        label="抓住你的手腕",
        npc_id="A",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她抓住你的手腕，没松开，也没解释。",
        when="他要走、或者你们刚吵完",
    ),
    ServiceAction(
        id="pull_close",
        label="拉住你的胳膊，把你拽近一点",
        npc_id="A",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她拉住你的胳膊，把你往她那边拽了一下，然后立刻松手。",
        when="他站得太远、或者要让开路的时候",
    ),
    ServiceAction(
        id="lean_shoulder",
        label="靠在你肩上待一会儿",
        npc_id="A",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她走过来靠在你肩上，只说了一句「就一会儿」，声音闷在布料里。",
        when="她刚跑完一趟、或者天晚了",
    ),
    ServiceAction(
        id="give_umbrella",
        label="把那把伞塞给你",
        npc_id="A",
        stage=Stage.AMBIGUOUS,
        scene="",
        prop="",
        outcome="她把那把断过骨、缠着胶带的黑伞塞到你手上。「下雨就撑。不用还。」",
        when="他要出远门、或者天要变了",
    ),
)

_BY_ID = {s.id: s for s in SERVICES}
_BY_NPC: dict[str, tuple[ServiceAction, ...]] = {
    npc: tuple(s for s in SERVICES if s.npc_id == npc) for npc in ("A", "B")
}


def get(action_id: str) -> ServiceAction | None:
    return _BY_ID.get(action_id)


def for_stage(npc_id: str, stage: Stage) -> list[ServiceAction]:
    """当前阶段能做的全部照料动作。"""
    level = _ORDER.index(stage)
    return [s for s in _BY_NPC.get(npc_id, ()) if _ORDER.index(s.stage) <= level]


def offer_script(action_id: str) -> dict:
    """提议阶段的脚本：走到玩家身边，问一句，然后停住等确认。

    只做"问"，不做"做"。真正的执行在玩家点确认之后（`perform_script`）。
    """
    return {
        "op": "seq",
        "children": [
            {"op": "move", "place": "$player"},
            {"op": "offer", "action": action_id},
        ],
    }


def perform_script(action: ServiceAction) -> dict:
    """执行脚本：走开 → 停一下 → 回来 → 交付。"""
    steps: list[dict] = []
    if action.scene:
        steps.append({"op": "move", "place": action.scene})
        if action.prop:
            steps.append(
                {
                    "op": "move",
                    "place": action.scene,
                    "to_prop": action.prop,
                }
            )
        steps.append({"op": "wait", "seconds": 1.8})
        steps.append({"op": "move", "place": "$player"})
    else:
        # 原地演：站住一会儿再开口，别让动作和台词同时发生
        steps.append({"op": "wait", "seconds": 1.2})

    steps.append({"op": "deliver", "action": action.id, "outcome": action.outcome})
    return {"op": "seq", "children": steps}
