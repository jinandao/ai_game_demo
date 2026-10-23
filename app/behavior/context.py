"""行为域：运行时上下文（Agent、事件、说话请求）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .world import World


@dataclass
class Agent:
    npc_id: str
    place: str                          # 场景 id（沿用旧字段名，减少上游改动）
    x: float = 720.0                    # 场景内坐标
    y: float = 600.0
    target: str | None = None           # 目标场景
    target_xy: tuple[float, float] | None = None
    facing: str = "down"                # up / down / left / right
    speed: float = 150.0                # px/s
    posture: str = "stand"              # stand | sit | lie | lean
    busy: bool = False
    walking: bool = False
    blackboard: dict[str, Any] = field(default_factory=dict)

    @property
    def pos(self) -> tuple[float, float]:
        return self.x, self.y


@dataclass(frozen=True)
class SpeakRequest:
    """让 NPC 说一句话的请求。只有 topic / action，没有台词正文。"""

    npc_id: str
    topic: str                          # greet | invite | share_memory | observe
    about: str | None = None            # 指向哪条记忆
    action: str | None = None           # invite 专用：invite_walk / request_hug ...


@dataclass(frozen=True)
class WorldEvent:
    """行为层抛给外界的信号。UI 和对话层都监听它。"""

    kind: str                           # move_start / move_end / say / invite / posture
    npc_id: str
    payload: dict[str, Any] = field(default_factory=dict)


# 说话能力的注入点。默认实现接 DialoguePipeline，测试时换成 lambda。
SpeechProvider = Callable[[SpeakRequest], str]
EventSink = Callable[[WorldEvent], None]


@dataclass
class BehaviorContext:
    agent: Agent
    world: World
    player_place: str
    dt: float
    now: datetime
    speech: SpeechProvider | None = None
    emit: EventSink | None = None

    def speak(self, req: SpeakRequest) -> str:
        text = self.speech(req) if self.speech else ""
        self.signal(WorldEvent("say", req.npc_id, {"text": text, "topic": req.topic,
                                                   "action": req.action}))
        return text

    def signal(self, event: WorldEvent) -> None:
        if self.emit:
            self.emit(event)

    def child(self, dt: float) -> BehaviorContext:
        """同一 tick 内给子节点用的轻量副本（只换 dt）。"""
        return BehaviorContext(
            agent=self.agent,
            world=self.world,
            player_place=self.player_place,
            dt=dt,
            now=self.now,
            speech=self.speech,
            emit=self.emit,
        )
