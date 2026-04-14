import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from traceloop.sdk.decorators import workflow
from opentelemetry import trace

from otel_setup import init_observability, GenAIMetrics

# ---------------------------------------------------------------------------
# Bootstrap observability (metrics → logs → tracing, order matters)
# ---------------------------------------------------------------------------
otel = init_observability("local-llm-backend")

logger = otel.logger
m = GenAIMetrics(otel.meter)
_tracer = otel.tracer

app = FastAPI(title="Local LLM API Bridge", version="1.0.0")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemma4:26b")

_parsed_ollama = urlparse(OLLAMA_BASE_URL)
_OLLAMA_HOST = _parsed_ollama.hostname or "localhost"
_OLLAMA_PORT = _parsed_ollama.port or 11434

# Model-name prefix → provider mapping
_PROVIDER_PATTERNS: list[tuple[list[str], str]] = [
    (["gpt-", "o1-", "o3-", "o4-", "dall-e", "text-embedding"], "openai"),
    (["claude-"], "anthropic"),
    (["gemini-"], "google"),
    (["copilot-", "github/"], "github.copilot"),
    (["mistral-", "mixtral-", "codestral-"], "mistral"),
    (["command-", "embed-"], "cohere"),
    (["deepseek-"], "deepseek"),
]

_MAX_CONTENT_LEN = 500  # Truncate prompt/response in span events


def _detect_provider(model: str) -> str:
    model_lower = model.lower()
    for prefixes, provider in _PROVIDER_PATTERNS:
        if any(model_lower.startswith(p) for p in prefixes):
            return provider
    return "ollama"


def _classify_request(messages: list[dict]) -> str:
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


def _semconv_attrs(model: str) -> dict:
    """Common GenAI semconv metric attributes."""
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": _detect_provider(model),
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "server.address": _OLLAMA_HOST,
        "server.port": _OLLAMA_PORT,
    }


def _set_genai_span(span, model: str, request_type: str, stream: bool,
                    messages: list[dict], extra_ctx: dict):
    """Set all gen_ai.* span attributes and input span events."""
    provider = _detect_provider(model)

    # Span name: e.g. "User Chat · gemma4:26b"
    span.update_name(f"{request_type} · {model}")

    # --- Required gen_ai.* attributes (OTel GenAI semconv) ---
    span.set_attribute("gen_ai.system", provider)
    span.set_attribute("gen_ai.provider.name", provider)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("llm.request.type", "chat")
    span.set_attribute("llm.is_streaming", stream)
    span.set_attribute("llm.request.purpose", request_type)
    span.set_attribute("server.address", _OLLAMA_HOST)
    span.set_attribute("server.port", _OLLAMA_PORT)

    # --- Optional request params ---
    if extra_ctx.get("temperature") is not None:
        span.set_attribute("gen_ai.request.temperature", extra_ctx["temperature"])
    if extra_ctx.get("top_p") is not None:
        span.set_attribute("gen_ai.request.top_p", extra_ctx["top_p"])
    if extra_ctx.get("max_tokens") is not None:
        span.set_attribute("gen_ai.request.max_tokens", extra_ctx["max_tokens"])

    # --- Indexed prompt attributes (for Dynatrace) ---
    for msg in reversed(messages):
        if msg.get("role") == "user":
            span.set_attribute("gen_ai.prompt.0.role", "user")
            span.set_attribute("gen_ai.prompt.0.content", msg.get("content", ""))
            break

    # --- Span event: gen_ai.user.message (OTel best practice) ---
    for msg in reversed(messages):
        if msg.get("role") == "user":
            span.add_event("gen_ai.user.message", {
                "gen_ai.prompt.role": "user",
                "gen_ai.prompt.content": _truncate(msg.get("content", "")),
            })
            break

    # --- Correlation ---
    user_msgs = [msg["content"] for msg in messages if msg["role"] == "user"]
    fingerprint_input = user_msgs[0][:200] if (request_type != "User Chat" and user_msgs) else "|".join(user_msgs)
    span.set_attribute("conversation.fingerprint",
                       hashlib.sha256(fingerprint_input.encode()).hexdigest()[:12])
    auth_header = extra_ctx.get("auth_header", "")
    if auth_header:
        span.set_attribute("enduser.id", hashlib.sha256(auth_header.encode()).hexdigest()[:8])


def _set_genai_response(span, content: str, model: str,
                        prompt_toks: int, completion_toks: int,
                        finish_reason: str = "stop", resp_id: str = ""):
    """Set response attributes and span event on the gen_ai.chat span."""
    # --- Indexed completion attributes (for Dynatrace) ---
    span.set_attribute("gen_ai.completion.0.role", "assistant")
    span.set_attribute("gen_ai.completion.0.content", content)
    span.set_attribute("gen_ai.completion.0.finish_reason", finish_reason)

    # --- Required response attributes ---
    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute("gen_ai.response.finish_reasons", json.dumps([finish_reason]))
    span.set_attribute("gen_ai.usage.input_tokens", prompt_toks)
    span.set_attribute("gen_ai.usage.output_tokens", completion_toks)
    span.set_attribute("gen_ai.usage.prompt_tokens", prompt_toks)
    span.set_attribute("gen_ai.usage.completion_tokens", completion_toks)
    if resp_id:
        span.set_attribute("gen_ai.response.id", resp_id)

    # --- Span event: gen_ai.assistant.message ---
    span.add_event("gen_ai.assistant.message", {
        "gen_ai.completion.role": "assistant",
        "gen_ai.completion.content": _truncate(content),
        "gen_ai.completion.finish_reason": finish_reason,
    })


def _record_metrics(attrs: dict, model: str, duration: float,
                    prompt_toks: int, completion_toks: int,
                    ttft: float | None = None, tpot: float | None = None):
    m.operation_duration.record(duration, attrs)
    m.token_usage.record(prompt_toks, {**attrs, "gen_ai.token.type": "input"})
    m.token_usage.record(completion_toks, {**attrs, "gen_ai.token.type": "output"})
    if ttft is not None:
        m.ttft.record(ttft, attrs)
    if tpot is not None:
        m.tpot.record(tpot, attrs)
    if duration > 0 and completion_toks > 0:
        m.token_throughput.record(completion_toks / duration, attrs)
    m.active_requests.add(-1, {"model": model})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Pydantic schemas
# --------------------------------------------------------------------------- #

class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            ollama_models = response.json().get("models", [])
            return {
                "object": "list",
                "data": [
                    {"id": mdl["name"], "object": "model",
                     "created": int(time.time()), "owned_by": "ollama"}
                    for mdl in ollama_models
                ],
            }
        except httpx.ConnectError:
            raise HTTPException(503, "Cannot reach Ollama.")
        except Exception as exc:
            raise HTTPException(503, str(exc))


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    """Thin router — delegates to the @workflow helpers that own the span."""
    model = request.model or DEFAULT_MODEL
    messages_raw = [{"role": msg.role, "content": msg.content} for msg in request.messages]

    m.request_count.add(1, {"model": model, "stream": str(request.stream)})
    m.active_requests.add(1, {"model": model})

    extra_ctx = {
        "auth_header": raw_request.headers.get("authorization", ""),
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
    }

    ollama_payload: dict = {
        "model": model,
        "messages": messages_raw,
        "stream": request.stream,
        "options": {},
    }
    if request.temperature is not None:
        ollama_payload["options"]["temperature"] = request.temperature
    if request.top_p is not None:
        ollama_payload["options"]["top_p"] = request.top_p
    if request.max_tokens is not None:
        ollama_payload["options"]["num_predict"] = request.max_tokens

    if request.stream:
        return StreamingResponse(
            _stream_ollama(ollama_payload, model, messages_raw, extra_ctx),
            media_type="text/event-stream",
        )
    return await _non_stream_ollama(ollama_payload, model, messages_raw, extra_ctx)


# --------------------------------------------------------------------------- #
# gen_ai.chat span — streaming
# --------------------------------------------------------------------------- #

@workflow(name="gen_ai.chat")
async def _stream_ollama(payload: dict, model: str,
                         messages: list[dict], extra_ctx: dict):
    stream_start = time.time()
    first_token_time: float | None = None
    request_type = _classify_request(messages)
    attrs = _semconv_attrs(model)
    span = trace.get_current_span()

    # --- Set all input attributes + span event ---
    _set_genai_span(span, model, request_type, True, messages, extra_ctx)
    m.message_count.record(len(messages), attrs)

    logger.info("Streaming start", extra={"model": model, "request_type": request_type})

    full_response_parts: list[str] = []
    chunk_count = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        # --- http.client.request child span to Ollama ---
        with _tracer.start_as_current_span(
            f"POST {_OLLAMA_HOST}:{_OLLAMA_PORT}/api/chat",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "http.request.method": "POST",
                "url.full": f"{OLLAMA_BASE_URL}/api/chat",
                "server.address": _OLLAMA_HOST,
                "server.port": _OLLAMA_PORT,
            },
        ) as http_span:
            async with client.stream(
                "POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload
            ) as response:
                http_span.set_attribute("http.response.status_code", response.status_code)

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    content = chunk.get("message", {}).get("content", "")
                    done = chunk.get("done", False)
                    if content:
                        full_response_parts.append(content)
                        chunk_count += 1
                        if first_token_time is None:
                            first_token_time = time.time()

                    openai_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": content} if content else {},
                            "finish_reason": "stop" if done else None,
                        }],
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"

                    if done:
                        duration = time.time() - stream_start
                        prompt_toks = chunk.get("prompt_eval_count", 0)
                        completion_toks = chunk.get("eval_count", 0)
                        full_response = "".join(full_response_parts)

                        # --- Response attributes + span event ---
                        _set_genai_response(span, full_response, model,
                                            prompt_toks, completion_toks)

                        # TTFT / TPOT
                        ttft = (first_token_time - stream_start) if first_token_time else None
                        tpot = None
                        if first_token_time and completion_toks > 1:
                            tpot = (time.time() - first_token_time) / (completion_toks - 1)

                        _record_metrics(attrs, model, duration, prompt_toks,
                                        completion_toks, ttft, tpot)
                        m.stream_chunks.add(chunk_count, {"model": model})

                        logger.info("Stream done", extra={
                            "model": model, "duration_s": round(duration, 3),
                            "prompt_tokens": prompt_toks,
                            "completion_tokens": completion_toks,
                        })

                        yield "data: [DONE]\n\n"
                        break


# --------------------------------------------------------------------------- #
# gen_ai.chat span — non-streaming
# --------------------------------------------------------------------------- #

@workflow(name="gen_ai.chat")
async def _non_stream_ollama(payload: dict, model: str,
                             messages: list[dict], extra_ctx: dict) -> dict:
    ollama_start = time.time()
    request_type = _classify_request(messages)
    attrs = _semconv_attrs(model)
    span = trace.get_current_span()

    # --- Set all input attributes + span event ---
    _set_genai_span(span, model, request_type, False, messages, extra_ctx)
    m.message_count.record(len(messages), attrs)

    # --- http.client.request child span to Ollama ---
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            with _tracer.start_as_current_span(
                f"POST {_OLLAMA_HOST}:{_OLLAMA_PORT}/api/chat",
                kind=trace.SpanKind.CLIENT,
                attributes={
                    "http.request.method": "POST",
                    "url.full": f"{OLLAMA_BASE_URL}/api/chat",
                    "server.address": _OLLAMA_HOST,
                    "server.port": _OLLAMA_PORT,
                },
            ) as http_span:
                response = await client.post(
                    f"{OLLAMA_BASE_URL}/api/chat", json=payload
                )
                response.raise_for_status()
                http_span.set_attribute("http.response.status_code", response.status_code)
        except httpx.ConnectError:
            logger.error("Cannot reach Ollama at %s", OLLAMA_BASE_URL)
            span.set_attribute("error.type", "ConnectError")
            m.error_count.add(1, {**attrs, "error.type": "ConnectError"})
            m.active_requests.add(-1, {"model": model})
            raise HTTPException(503, "Cannot reach Ollama.")

    data = response.json()
    content = data.get("message", {}).get("content", "")
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)
    resp_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    # --- Response attributes + span event ---
    _set_genai_response(span, content, model, prompt_tokens, completion_tokens,
                        resp_id=resp_id)

    # TTFT / TPOT from Ollama timing (nanoseconds)
    prompt_eval_duration = data.get("prompt_eval_duration", 0)
    eval_duration = data.get("eval_duration", 0)
    ttft = (prompt_eval_duration / 1e9) if prompt_eval_duration > 0 else None
    tpot = None
    if eval_duration > 0 and completion_tokens > 1:
        tpot = (eval_duration / 1e9) / (completion_tokens - 1)

    duration = time.time() - ollama_start
    _record_metrics(attrs, model, duration, prompt_tokens, completion_tokens, ttft, tpot)

    logger.info("Chat done", extra={"model": model, "duration_s": round(duration, 3),
                                     "prompt_tokens": prompt_tokens,
                                     "completion_tokens": completion_tokens})

    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
