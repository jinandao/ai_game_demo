"""最小 CLI 入口，不启动 Pygame。

    python run.py            # 连本地 ollama
    python run.py --mock     # 不连模型

    会话内命令：/end /state /aff N /time /idle /tick N /where /events
"""

from __future__ import annotations

import sys

from app.factory import build
from app.infra.llm import MockLLM
from app.infra.clock import FrozenClock

REPLIES = {
    "stylize": "（这里是她的话）",
    "summary": "玩家提到了自己的事。",
}


def main() -> None:
    mock = "--mock" in sys.argv
    app = build(
        llm=MockLLM(REPLIES) if mock else None,
        clock=FrozenClock() if mock else None,
        use_embedding=not mock,        # mock 不连 ollama，进程要秒开
    )

    npc_id = "A"
    session = app.enter_chat(npc_id)
    print(f"[{app.deps.personas[npc_id].name}] 会话已开。输入 /end 结束。\n")

    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue
        if text == "/end":
            report = app.close_chat()
            print(f"[会话结束] {report}")
            session = app.enter_chat(npc_id)
            print(f"[{app.deps.personas[npc_id].name}] 新的会话。\n")
            continue
        if text == "/state":
            print(app.debug_dump())
            continue
        if text.startswith("/aff "):
            app.debug_set_affection(npc_id, int(text.split()[1]))
            print(app.debug_dump())
            continue
        if text == "/time":
            app.debug_advance(days=1)
            print("时间推进了一天。")
            continue
        if text == "/idle":
            app.idle_tick()
            events = app.flush_decisions()
            if not events:
                print("[决策] 队列超时，没有结果")
            for event in events:
                print(f"[决策] {event.npc_id} → {event.decision.intent.value}")
            for snap in app.behavior.snapshot():
                print(f"  {snap['npc_id']}: {snap['place']} / {snap['label'] or '-'}")
            continue
        if text.startswith("/tick"):
            parts = text.split()
            seconds = float(parts[1]) if len(parts) > 1 else 10.0
            step = 0.5
            elapsed = 0.0
            while elapsed < seconds:
                app.tick(step)
                elapsed += step
            for e in app.drain_events():
                if e.kind in ("say", "move_end", "invite"):
                    print(f"  [{e.kind}] {e.payload}")
            print(f"[已推进 {seconds}s]")
            continue
        if text == "/where":
            for snap in app.behavior.snapshot():
                print(snap)
            continue
        if text == "/events":
            for e in app.drain_events():
                print(e)
            continue

        turn = session.send(text)
        print(f"{app.deps.personas[npc_id].name}：{turn.text}\n")


if __name__ == "__main__":
    main()
