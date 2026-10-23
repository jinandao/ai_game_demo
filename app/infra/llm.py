"""基础设施：LLM 客户端，全项目唯一的模型调用入口。

只依赖标准库 urllib；保留 MockLLM 用于离线流程测试。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class LLMError(RuntimeError):
    pass


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        on_delta=None,
    ) -> str:
        """普通文本补全。给了 on_delta 就走流式，逐块回调。"""

    @abstractmethod
    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """结构化输出。解析失败抛 LLMError，由调用方决定是否降级。"""

    def ping(self) -> bool:
        """模型是否可达。UI 启动时用来提示，不参与业务逻辑。"""
        return True


# ─────────────────────────── Mock ───────────────────────────


class MockLLM(LLMClient):
    """按 prompt 关键词匹配预置响应，用于流程测试。

    scripted: {关键词: 响应}。按插入顺序匹配，命中即返回。
    """

    def __init__(self, scripted: dict[str, Any], *, json_default: Any = None) -> None:
        self._scripted = scripted
        self._json_default = json_default
        self.calls: list[list[dict[str, str]]] = []

    def _match(self, messages: list[dict[str, str]]) -> Any:
        blob = "\n".join(m.get("content", "") for m in messages)
        for keyword, response in self._scripted.items():
            if keyword in blob:
                return response
        return self._json_default

    def complete(self, messages, *, temperature=0.7, max_tokens=None, on_delta=None) -> str:
        self.calls.append(messages)
        resp = self._match(messages)
        text = (
            "……（mock：没有匹配到预置回复）"
            if resp is None
            else (resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False))
        )
        if on_delta is not None:
            for i in range(0, len(text), 4):      # 模拟流式，方便离线调 UI
                on_delta(text[i : i + 4])
        return text

    def complete_json(
        self, messages, *, schema, temperature=0.0, max_tokens=None
    ) -> dict[str, Any]:
        self.calls.append(messages)
        resp = self._match(messages)
        if resp is None:
            raise LLMError("MockLLM: no scripted response matched")
        if isinstance(resp, dict):
            return resp
        try:
            return json.loads(resp)
        except json.JSONDecodeError as e:
            raise LLMError(f"MockLLM: 非 JSON 响应 {resp!r}") from e


# ─────────────────────────── Ollama ───────────────────────────


class OllamaClient(LLMClient):
    """本地 ollama。结构化输出优先 format=schema，失败退回 format=json。"""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str = "http://localhost:11434",
        timeout: float = 180.0,
        keep_alive: str = "30m",
        num_ctx: int = 8192,
        think: bool | None = False,
    ) -> None:
        # 系统提示词约 4000 字符，num_ctx 低于 4096 会被截断。
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        # 推理模型默认先输出思考段，会占满 num_predict 并截断 JSON。
        # None 表示不传该字段，兼容老版本 ollama。
        self.think = think

    # ── 公共 ──

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

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
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise LLMError(f"ollama HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise LLMError(f"ollama 连接失败: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError("ollama 返回的不是 JSON") from e

    def _chat(self, messages, *, temperature, max_tokens, fmt, on_delta=None) -> str:
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_ctx": self.num_ctx,
        }
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # keep_alive 让模型留在显存里，否则每次调用都要重新加载（几秒起步）
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        if fmt is not None:
            payload["format"] = fmt

        if on_delta is not None:
            payload["stream"] = True
            return self._stream("/api/chat", payload, on_delta)

        data = self._post("/api/chat", payload)
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise LLMError("ollama 返回空内容")
        return content

    def _stream(self, path: str, payload: dict[str, Any], on_delta) -> str:
        """逐行读 ndjson，每块回调一次。返回拼完的全文。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("error"):
                        raise LLMError(f"ollama 流式错误: {data['error']}")
                    chunk = (data.get("message") or {}).get("content", "")
                    if chunk:
                        parts.append(chunk)
                        on_delta(chunk)
                    if data.get("done"):
                        break
        except LLMError:
            raise
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise LLMError(f"ollama HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise LLMError(f"ollama 流式连接失败: {e}") from e

        text = "".join(parts)
        if not text.strip():
            raise LLMError("ollama 返回空内容")
        return text

    # ── 实现 ──

    def complete(self, messages, *, temperature=0.7, max_tokens=None, on_delta=None) -> str:
        return self._chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            fmt=None,
            on_delta=on_delta,
        )

    def complete_json(
        self, messages, *, schema, temperature=0.0, max_tokens=None
    ) -> dict[str, Any]:
        try:
            raw = self._chat(
                messages, temperature=temperature, max_tokens=max_tokens, fmt=schema
            )
        except LLMError:
            raw = self._chat(
                messages, temperature=temperature, max_tokens=max_tokens, fmt="json"
            )
        return _loads_json(raw)


# ─────────────────────────── OpenAI 兼容 ───────────────────────────


class OpenAIClient(LLMClient):
    """OpenAI 及一切兼容 /v1/chat/completions 的服务（vLLM、DeepSeek、通义…）。"""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise LLMError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise LLMError(f"连接失败: {e}") from e

    def _chat(self, messages, *, temperature, max_tokens, json_mode) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构异常: {str(data)[:200]}") from e

    def complete(self, messages, *, temperature=0.7, max_tokens=None, on_delta=None) -> str:
        # 这里不做流式：OpenAI 兼容端的流式格式各家有差异，聊天台词本来就短
        return self._chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=False
        )

    def complete_json(
        self, messages, *, schema, temperature=0.0, max_tokens=None
    ) -> dict[str, Any]:
        # json_object 模式要求 prompt 里出现 "json" 字样，调用方的提示词都已满足
        raw = self._chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        return _loads_json(raw)


# ─────────────────────────── 工具 ───────────────────────────


def _loads_json(raw: str) -> dict[str, Any]:
    """容忍模型在 JSON 外面包一层 ```json 或说明文字。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError(f"无法解析为 JSON: {raw[:200]}")
