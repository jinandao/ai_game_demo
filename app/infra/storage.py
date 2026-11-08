"""文件存储，保证写入原子性。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

class JsonFile:
    """整文件读写。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Any:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # demo 里手改配置很常见，坏文件不阻塞启动
            return {}

    def save(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

class JsonlFile:
    """追加写。永不重写整个文件——半写坏一行的代价远小于丢整个文件。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # 跳过坏行，不让一行毁掉全部记忆
