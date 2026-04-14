"""GenAI span attribute helpers and request classification."""

from __future__ import annotations

import hashlib
import json

from opentelemetry.trace import Span

from llm_otel_kit.metrics import GenAIMetrics

# ---------------------------------------------------------------------------
# Provider detection from model name
# ---------------------------------------------------------------------------
_PROVIDER_PATTERNS: list[tuple[list[str], str]] = [
    (["gpt-", "o1-", "o3-", "o4-", "dall-e", "text-embedding"], "openai"),
    (["claude-"], "anthropic"),
    (["gemini-"], "google"),
    (["copilot-", "github/"], "github.copilot"),
    (["mistral-", "mixtral-", "codestral-"], "mistral"),
    (["command-", "embed-"], "cohere"),
    (["deepseek-"], "deepseek"),
]

_MAX_CONTENT_LEN = 500


def detect_provider(model: str) -> str:
    """Infer ``gen_ai.system`` from model name prefix."""
    model_lower = model.lower()
    for prefixes, provider in _PROVIDER_PATTERNS:
        if any(model_lower.startswith(p) for p in prefixes):
            return provider
    return "ollama"


def classify_request(messages: list[dict]) -> str:
    """Classify an OpenAI-format message list into a purpose label."""
    last_content = (messages[-1].get("content", "") if messages else "").lower()
    if "generate a concise" in last_content and "title" in last_content:
        return "Title Generation"
    if ("generate tags" in last_content or "categorize" in last_content
            or "tag the conversation" in last_content):
        return "Tag Generation"
    if "follow-up" in last_content or ("suggest" in last_content and "question" in last_content):
        return "Suggestion Generation"
    if messages and all(msg.get("role") == "system" for msg in messages):
        return "System Prompt"
    return "User Chat"


def _truncate(text: str) -> str:
    return text[:_MAX_CONTENT_LEN] + "..." if len(text) > _MAX_CONTENT_LEN else text


# ---------------------------------------------------------------------------
# Span attribute setters (OTel GenAI semconv)
# ---------------------------------------------------------------------------

def semconv_attrs(model: str, server_host: str, server_port: int) -> dict:
    """Build the standard GenAI metric attribute dict."""
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": detect_provider(model),
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "server.address": server_host,
        "server.port": server_port,
    }


def set_genai_span(
    span: Span,
    model: str,
    request_type: str,
    stream: bool,
    messages: list[dict],
    server_host: str,
    server_port: int,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    auth_header: str = "",
) -> None:
    """Set all gen_ai.* request attributes and input span event."""
    provider = detect_provider(model)

    span.update_name(f"{request_type} · {model}")

    span.set_attribute("gen_ai.system", provider)
    span.set_attribute("gen_ai.provider.name", provider)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("llm.request.type", "chat")
    span.set_attribute("llm.is_streaming", stream)
    span.set_attribute("llm.request.purpose", request_type)
    span.set_attribute("server.address", server_host)
    span.set_attribute("server.port", server_port)

    if temperature is not None:
        span.set_attribute("gen_ai.request.temperature", temperature)
    if top_p is not None:
        span.set_attribute("gen_ai.request.top_p", top_p)
    if max_tokens is not None:
        span.set_attribute("gen_ai.request.max_tokens", max_tokens)

    # Indexed prompt attribute (last user message)
    for msg in reversed(messages):
        if msg.get("role") == "user":
            span.set_attribute("gen_ai.prompt.0.role", "user")
            span.set_attribute("gen_ai.prompt.0.content", msg.get("content", ""))
            break

    # Span event
    for msg in reversed(messages):
        if msg.get("role") == "user":
            span.add_event("gen_ai.user.message", {
                "gen_ai.prompt.role": "user",
                "gen_ai.prompt.content": _truncate(msg.get("content", "")),
            })
            break

    # Conversation fingerprint
    user_msgs = [msg["content"] for msg in messages if msg["role"] == "user"]
    fp_input = user_msgs[0][:200] if (request_type != "User Chat" and user_msgs) else "|".join(user_msgs)
    span.set_attribute("conversation.fingerprint",
                       hashlib.sha256(fp_input.encode()).hexdigest()[:12])

    if auth_header:
        span.set_attribute("enduser.id",
                           hashlib.sha256(auth_header.encode()).hexdigest()[:8])


def set_genai_response(
    span: Span,
    content: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    finish_reason: str = "stop",
    response_id: str = "",
) -> None:
    """Set response attributes and assistant span event."""
    span.set_attribute("gen_ai.completion.0.role", "assistant")
    span.set_attribute("gen_ai.completion.0.content", content)
    span.set_attribute("gen_ai.completion.0.finish_reason", finish_reason)

    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute("gen_ai.response.finish_reasons", json.dumps([finish_reason]))
    span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
    span.set_attribute("gen_ai.usage.prompt_tokens", prompt_tokens)
    span.set_attribute("gen_ai.usage.completion_tokens", completion_tokens)
    if response_id:
        span.set_attribute("gen_ai.response.id", response_id)

    span.add_event("gen_ai.assistant.message", {
        "gen_ai.completion.role": "assistant",
        "gen_ai.completion.content": _truncate(content),
        "gen_ai.completion.finish_reason": finish_reason,
    })


def record_metrics(
    m: GenAIMetrics,
    attrs: dict,
    model: str,
    duration: float,
    prompt_tokens: int,
    completion_tokens: int,
    ttft: float | None = None,
    tpot: float | None = None,
) -> None:
    """Record all GenAI + operational metrics for one completed request."""
    m.operation_duration.record(duration, attrs)
    m.token_usage.record(prompt_tokens, {**attrs, "gen_ai.token.type": "input"})
    m.token_usage.record(completion_tokens, {**attrs, "gen_ai.token.type": "output"})
    if ttft is not None:
        m.ttft.record(ttft, attrs)
    if tpot is not None:
        m.tpot.record(tpot, attrs)
    if duration > 0 and completion_tokens > 0:
        m.token_throughput.record(completion_tokens / duration, attrs)
    m.active_requests.add(-1, {"model": model})
