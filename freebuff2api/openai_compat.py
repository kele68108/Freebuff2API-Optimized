from __future__ import annotations

import time
import uuid
from typing import Any

from .cli_prompt import cli_system_prompt
from .codebuff import FreebuffSession
from .models import resolve_model


_UPSTREAM_CHAT_KEYS = frozenset(
    {
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "metadata",
        "modalities",
        "parallel_tool_calls",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
    }
)


def model_id(requested: str | None = None) -> str:
    return resolve_model(requested).upstream_id


def _contains_buffy_marker(
    messages: list[dict[str, Any]],
    prompt: str,
) -> bool:
    """True if any message already carries the CLI's Buffy system prompt.

    Mirrors freebuff-go's BuildUpstreamBody hasBuffy check: look for the
    prompt's first sentence in any message content so we never double-inject
    the system prompt when a client already sent it. The marker is derived
    from the actual prompt (first sentence, capped at 64 chars — same as
    freebuff-go's markerFor) so it tracks prompt drift automatically.
    """
    if prompt:
        first = prompt.strip().split(".")[0].split("\n")[0][:64]
        marker = first if first else "You are Buffy, the strategic coding assistant"
    else:
        marker = "You are Buffy, the strategic coding assistant"
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and marker in content:
            return True
        if isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                    and marker in part["text"]
                ):
                    return True
    return False


def normalize_chat_messages(
    messages: Any,
    *,
    cli_path: str = "",
) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []

    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = dict(message)
        item.pop("timestamp", None)
        if item.get("role") == "developer":
            item["role"] = "system"
        if item.get("role") == "system":
            item.setdefault("cache_control", {"type": "ephemeral"})
        normalized.append(item)

    # The upstream free_mode_cli_required gate fingerprints requests on the
    # CLI's system prompt. When the real prompt is extractable from the
    # installed CLI binary, prepend it as the leading system message unless the
    # client already sent it (mirrors freebuff-go BuildUpstreamBody).
    prompt = cli_system_prompt(cli_path)
    if prompt and not _contains_buffy_marker(normalized, prompt):
        normalized.insert(
            0,
            {
                "role": "system",
                "content": prompt,
                "cache_control": {"type": "ephemeral"},
            },
        )
    elif not prompt and not any(
        item.get("role") == "system" for item in normalized
    ):
        # Fallback when no CLI binary is available: keep a minimal Buffy
        # identity line so downstream consumers still see a system message.
        normalized.insert(
            0,
            {
                "role": "system",
                "content": "You are Buffy, the strategic coding assistant.",
                "cache_control": {"type": "ephemeral"},
            },
        )
    return normalized


def build_upstream_payload(
    body: dict[str, Any],
    *,
    session: FreebuffSession,
    run_id: str,
    client_id: str,
    trace_session_id: str | None = None,
    upstream_model_id: str | None = None,
    cli_path: str = "",
) -> dict[str, Any]:
    payload = {
        key: body[key]
        for key in _UPSTREAM_CHAT_KEYS
        if key in body and body[key] is not None
    }
    payload["model"] = upstream_model_id or model_id(body.get("model"))
    payload["messages"] = normalize_chat_messages(
        body.get("messages"),
        cli_path=cli_path,
    )
    payload["stream"] = True
    payload.setdefault("stop", ['"cb_easp"'])

    payload["provider"] = {"data_collection": "deny"}
    payload["codebuff_metadata"] = {
        "freebuff_instance_id": session.instance_id,
        "trace_session_id": trace_session_id or str(uuid.uuid4()),
        "run_id": run_id,
        "client_id": client_id,
        "cost_mode": "free",
    }
    return payload


def sanitize_stream_chunk(chunk: dict[str, Any]) -> dict[str, Any] | None:
    clean = {
        "id": chunk.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": chunk.get("object") or "chat.completion.chunk",
        "created": chunk.get("created") or int(time.time()),
        "model": chunk.get("model"),
        "choices": [],
    }
    if chunk.get("system_fingerprint"):
        clean["system_fingerprint"] = chunk["system_fingerprint"]
    if chunk.get("usage") is not None:
        clean["usage"] = chunk["usage"]

    for choice in chunk.get("choices") or []:
        item = {
            "index": choice.get("index", 0),
            "delta": dict(choice.get("delta") or {}),
            "finish_reason": choice.get("finish_reason"),
        }
        if choice.get("logprobs") is not None:
            item["logprobs"] = choice["logprobs"]
        reasoning_content = item["delta"].pop("reasoning_content", None)
        if item["delta"].get("content") is None:
            item["delta"].pop("content", None)
        if isinstance(reasoning_content, str):
            item["delta"]["reasoning_content"] = reasoning_content
        clean["choices"].append(item)

    if not clean["choices"] and clean.get("usage") is None:
        return None
    return clean


class CompletionAccumulator:
    def __init__(self, model: str) -> None:
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.model = model
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.system_fingerprint: str | None = None
        self.tool_calls: dict[int, dict[str, Any]] = {}

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def reasoning_content(self) -> str:
        return "".join(self.reasoning_parts)

    def add(self, chunk: dict[str, Any]) -> None:
        self.id = chunk.get("id") or self.id
        self.created = chunk.get("created") or self.created
        self.model = chunk.get("model") or self.model
        self.usage = chunk.get("usage") or self.usage
        self.system_fingerprint = chunk.get("system_fingerprint") or self.system_fingerprint

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            reasoning_content = delta.get("reasoning_content")
            if isinstance(content, str):
                self.content_parts.append(content)
            if isinstance(reasoning_content, str):
                self.reasoning_parts.append(reasoning_content)
            for tool_call in delta.get("tool_calls") or []:
                self._add_tool_call(tool_call)
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def _add_tool_call(self, tool_call: dict[str, Any]) -> None:
        index = int(tool_call.get("index", 0))
        current = self.tool_calls.setdefault(
            index,
            {
                "id": tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": tool_call.get("type") or "function",
                "function": {"name": "", "arguments": ""},
            },
        )
        if tool_call.get("id"):
            current["id"] = tool_call["id"]
        if tool_call.get("type"):
            current["type"] = tool_call["type"]

        function = tool_call.get("function") or {}
        if function.get("name"):
            current["function"]["name"] = function["name"]
        if function.get("arguments"):
            current["function"]["arguments"] += function["arguments"]

    def final_response(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content,
        }
        if self.tool_calls:
            message["tool_calls"] = [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ]
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content

        response = {
            "id": self.id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self.finish_reason or "stop",
                }
            ],
            "usage": self.usage
            or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if self.system_fingerprint:
            response["system_fingerprint"] = self.system_fingerprint
        return response
