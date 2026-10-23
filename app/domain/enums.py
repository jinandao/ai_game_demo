"""领域内核：枚举。零内部依赖，任何层都可以 import。"""

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
    """记忆分三类：EVENT / PLAYER / OTHER。约定用 MemoryItem.promise 标记。"""

    EVENT = "event"
    PLAYER = "player"
    OTHER = "other"


class Stage(str, Enum):
    STRANGER = "stranger"
    ACQUAINTANCE = "acquaintance"
    CLOSE = "close"
    AMBIGUOUS = "ambiguous"
    DEPENDENT = "dependent"


class ActionId(str, Enum):
    INVITE_WALK = "invite_walk"
    INVITE_VISIT = "invite_visit"
    REQUEST_HUG = "request_hug"


class DecisionIntent(str, Enum):
    """决策层白名单。LLM 只能在这个集合的子集里选，不能自由发挥。"""

    IDLE = "idle"
    WORK = "work"                   # 干活（默认状态）
    SOLO = "solo"                   # 独处：湖边、长椅、修理间角落
    SEEK_PLAYER = "seek_player"
    SHARE_MEMORY = "share_memory"   # 消费 unresolved 条目
    SERVE = "serve"                 # 照料玩家：具体做什么看 Decision.action
    INVITE_WALK = "invite_walk"
    INVITE_VISIT = "invite_visit"
    REQUEST_HUG = "request_hug"
    REST = "rest"                   # 回自己房间睡
    WITHDRAW = "withdraw"


class ExtractMode(str, Enum):
    COMPACT = "compact"  # 压缩时抽取，覆盖被压掉的 head
    FINAL = "final"      # 会话结束时抽取，覆盖尾部未抽取轮次
