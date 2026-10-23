"""基础设施：文本嵌入。失败时抛 EmbedError，由调用方决定降级。"""

from __future__ import annotations

import json
import math
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class EmbedError(RuntimeError):
    pass


class Embedder(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入。返回的向量已 L2 归一化。"""

    @property
    @abstractmethod
    def dim(self) -> int: ...


class OllamaEmbedder(Embedder):
    """走 ollama 的 /api/embed，新老两版接口都试，新版失败退回单条接口。"""

    def __init__(
        self,
        model: str = "qwen3-embedding:0.6b",
        host: str = "http://localhost:11434",
        timeout: float = 8.0,
        batch: int = 16,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.batch = max(1, batch)
        self._dim = 0

    @property
    def dim(self) -> int:
        return self._dim

    # ── 对外 ──

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out.extend(self._embed_batch(texts[i : i + self.batch]))
        if out and not self._dim:
            self._dim = len(out[0])
        return out

    # ── 内部 ──

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._via_new_api(texts)
        except EmbedError:
            return self._via_old_api(texts)

    def _via_new_api(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.model, "input": texts})
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbedError("ollama /api/embed 返回结构不符")
        return [_normalize(v) for v in vectors]

    def _via_old_api(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            data = self._post("/api/embeddings", {"model": self.model, "prompt": text})
            vec = data.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise EmbedError("ollama /api/embeddings 返回空向量")
            out.append(_normalize(vec))
        return out

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise EmbedError(f"embed HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise EmbedError(f"embed 连接失败: {e}") from e
        except json.JSONDecodeError as e:
            raise EmbedError("embed 返回的不是 JSON") from e


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 1e-12:
        return [0.0] * len(vec)
    return [float(x) / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """两边都已归一化，所以直接点积。"""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
