"""Pluggable LLM transport with a single backend-agnostic chat interface.

Both backends speak the same internal shapes:

  history item := one of
    {"role": "user",      "text": str}
    {"role": "assistant", "text": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "id": str, "name": str, "result": str}

  ToolCall := {"id": str, "name": str, "args": dict}

``LLMClient.chat(system, history, tools)`` returns an ``Assistant`` with
``text`` and ``tool_calls`` (empty list => final answer, stop the loop).

Pure stdlib (urllib). The ``anthropic`` SDK is *not* required even for the
Anthropic backend — we hit the REST API directly so there is no version
coupling and the same code path works offline against Ollama.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """LLM transport / protocol failure (unreachable, bad status, bad JSON)."""


@dataclass
class Assistant:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # [{id,name,args}]

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _http_json(url: str, payload: dict, headers: dict, timeout: int = 600) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"HTTP {e.code} from {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {url}: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise LLMError(f"non-JSON reply from {url}: {e}") from e


# --------------------------------------------------------------------------- #
#  Ollama backend (default) — POST /api/chat with native tool calling.        #
# --------------------------------------------------------------------------- #
class OllamaClient:
    def __init__(self, host: str, model: str, temperature: float, num_ctx: int):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx

    def _messages(self, system: str, history: list[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        for h in history:
            if h["role"] == "user":
                msgs.append({"role": "user", "content": h["text"]})
            elif h["role"] == "assistant":
                m = {"role": "assistant", "content": h.get("text", "")}
                if h.get("tool_calls"):
                    m["tool_calls"] = [
                        {"function": {"name": tc["name"], "arguments": tc["args"]}}
                        for tc in h["tool_calls"]
                    ]
                msgs.append(m)
            elif h["role"] == "tool":
                # Ollama matches tool replies positionally; name-tag for clarity.
                msgs.append({
                    "role": "tool",
                    "content": f"[{h['name']}]\n{h['result']}",
                })
        return msgs

    def chat(self, system: str, history: list[dict], tools: list[dict]) -> Assistant:
        payload = {
            "model": self.model,
            "messages": self._messages(system, history),
            "tools": [{"type": "function", "function": t} for t in tools],
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        data = _http_json(f"{self.host}/api/chat", payload,
                          {"Content-Type": "application/json"})
        msg = data.get("message", {})
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": f"call_{i}", "name": fn.get("name", ""),
                          "args": args or {}})
        return Assistant(text=msg.get("content", "") or "", tool_calls=calls)


# --------------------------------------------------------------------------- #
#  Anthropic backend — REST /v1/messages, native tool use.                     #
# --------------------------------------------------------------------------- #
class AnthropicClient:
    API = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str, max_tokens: int, temperature: float):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set (use --backend ollama for local)")

    def _messages(self, history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for h in history:
            if h["role"] == "user":
                out.append({"role": "user",
                            "content": [{"type": "text", "text": h["text"]}]})
            elif h["role"] == "assistant":
                content = []
                if h.get("text"):
                    content.append({"type": "text", "text": h["text"]})
                for tc in h.get("tool_calls", []):
                    content.append({"type": "tool_use", "id": tc["id"],
                                    "name": tc["name"], "input": tc["args"]})
                out.append({"role": "assistant", "content": content})
            elif h["role"] == "tool":
                blk = {"type": "tool_result", "tool_use_id": h["id"],
                       "content": h["result"]}
                # Merge consecutive tool results into one user turn.
                if out and out[-1]["role"] == "user" and isinstance(
                        out[-1]["content"], list) and out[-1]["content"] and \
                        out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(blk)
                else:
                    out.append({"role": "user", "content": [blk]})
        return out

    def chat(self, system: str, history: list[dict], tools: list[dict]) -> Assistant:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": self._messages(history),
            "tools": [{"name": t["name"], "description": t["description"],
                       "input_schema": t["parameters"]} for t in tools],
        }
        data = _http_json(self.API, payload, {
            "Content-Type": "application/json",
            "x-api-key": self.key,
            "anthropic-version": "2023-06-01",
        })
        text, calls = "", []
        for blk in data.get("content", []):
            if blk.get("type") == "text":
                text += blk.get("text", "")
            elif blk.get("type") == "tool_use":
                calls.append({"id": blk["id"], "name": blk["name"],
                              "args": blk.get("input", {})})
        return Assistant(text=text, tool_calls=calls)


def build_client(cfg) -> "OllamaClient | AnthropicClient":
    if cfg.backend == "anthropic":
        return AnthropicClient(cfg.anthropic_model, cfg.max_tokens, cfg.temperature)
    if cfg.backend == "ollama":
        return OllamaClient(cfg.ollama_host, cfg.model, cfg.temperature, cfg.num_ctx)
    raise LLMError(f"unknown backend {cfg.backend!r} (use 'ollama' or 'anthropic')")
