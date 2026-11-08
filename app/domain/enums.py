"""枚举。"""

from __future__ import annotations

from enum import Enum

class Intent(str, Enum):
    """一轮对话的路由结果。只允许这五个值，非法值一律降级 CHITCHAT。"""

    CHITCHAT = "chitchat"        # 闲聊 / 软细节 -> 直通，不查任何库
    FACT_QUERY = "fact_query"    # 世界观 / 角色背景 -> L3
    RECALL = "recall"            # 玩家提及过去 -> L2
    ACTION = "action"            # 请求或回应动作 -> 阶段闸门
    OUT_OF_SCOPE = "out_of_scope"  # 越界 -> 角色化拒答

class Scope(str, Enum):
    WORLD = "world"          # 世界设定 / 地名 / 组织，全角色共享
    CHARACTER = "character"  # 角色背景，按 npc_id 隔离
    RULE = "rule"            # 游戏规则 / 数值

class Subject(str, Enum):
    """记忆只有三类，这是刻意的收敛。"""

    EVENT = "event"
    PLAYER = "player"
    OTHER = "other"

class MemoryTier(str, Enum):
    """记忆的三档，按"要不要长期留着"分，和 Subject 正交。"""

    EVENT = "event"
    INTERACTION = "interaction"
    DEEP = "deep"

class Stage(str, Enum):
    STRANGER = "stranger"
    ACQUAINTANCE = "acquaintance"
    CLOSE = "close"
    AMBIGUOUS = "ambiguous"
    DEPENDENT = "dependent"

class Mood(str, Enum):
    """她此刻的心情。"""

    LOW = "心情不佳"
    ANGRY = "生气"
    SAD = "难过"
    HAPPY = "开心"
    SHY = "害羞"
    BLISS = "幸福"

MOOD_VALUES = tuple(m.value for m in Mood)

class ActionId(str, Enum):
    INVITE_WALK = "invite_walk"
    INVITE_VISIT = "invite_visit"
    REQUEST_HUG = "request_hug"

class DecisionIntent(str, Enum):
    """决策层白名单：她"想达成什么"。"""

    # ── 日常：维持存在感，不打扰玩家 ──
    IDLE = "idle"                   # 就地发呆
    WORK = "work"                   # 在原地/附近忙自己的（踱步、整理）
    SHIFT = "shift"                 # 去摊上/食堂上工，被人看见在干活
    TEND = "tend"                   # 打理自己的地方：擦工作台、码零件
    SOLO = "solo"                   # 一个人待着：看景、坐着想事情
    REST = "rest"                   # 回自己房间躺下
    WITHDRAW = "withdraw"           # 体力见底，躲起来谁也不见

    # ── 生产：有明确产出的 ──
    GATHER = "gather"               # 去废墟翻材料
    PICK_FLOWER = "pick_flower"     # 去花园摘点东西
    COOK = "cook"                   # 在后厨做吃的
    CRAFT = "craft"                 # 在矮桌上做配件
    REPAIR = "repair"               # 在工作台修东西
    EAT = "eat"                     # 自己去吃点东西

    # ── 商业 ──
    SHOP_BUY = "shop_buy"           # 去大厅买点什么（补给/给他带）
    SHOP_SELL = "shop_sell"         # 把攒的东西卖掉换票据

    # ── 探索 ──
    PATROL = "patrol"               # 巡一圈，看看各处
    SEARCH = "search"               # 在一个地方仔细搜一遍

    # ── 对玩家：这几种会被玩家看见，也会打断他 ──
    SEEK_PLAYER = "seek_player"     # 主动走过去说话
    SHARE_MEMORY = "share_memory"   # 提起一件记着的事/约定
    SERVE = "serve"                 # 提议为他做点什么（具体哪件看 decided action）
    INVITE_WALK = "invite_walk"     # 问他要不要一起走走
    INVITE_VISIT = "invite_visit"   # 邀他去自己那儿
    REQUEST_HUG = "request_hug"     # 靠得近一点

class ExtractMode(str, Enum):
    COMPACT = "compact"  # 压缩时抽取，覆盖被压掉的 head
    FINAL = "final"      # 会话结束时抽取，覆盖尾部未抽取轮次
