"""提示词集中管理，只产出字符串。"""

from .behavior import BEHAVIOR_FORMAT, DECISION_SYSTEM, build_decision_system
from .chat import CORE_RULES, KIND_TASK, build_chat_system, build_chat_user
from .memory import EXTRACT_RULES, MODE_NOTE, build_extract_system, build_extract_user
from .persona import action_block, memory_block, persona_block
from .routing import ROUTER_SYSTEM, build_router_user

__all__ = [
    "BEHAVIOR_FORMAT",
    "CORE_RULES",
    "DECISION_SYSTEM",
    "EXTRACT_RULES",
    "KIND_TASK",
    "MODE_NOTE",
    "ROUTER_SYSTEM",
    "action_block",
    "build_chat_system",
    "build_chat_user",
    "build_decision_system",
    "build_extract_system",
    "build_extract_user",
    "build_router_user",
    "memory_block",
    "persona_block",
]
