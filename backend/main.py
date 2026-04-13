import hashlib
import json
import logging
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
from traceloop.sdk import Traceloop
from traceloop.sdk.decorators import workflow

from opentelemetry.sdk.metrics import MeterProvider, Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
    AggregationTemporality,
)
from opentelemetry import metrics, trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry._logs import set_logger_provider

# ---------------------------------------------------------------------------
# OTel Metrics — set up BEFORE Traceloop.init() so it doesn't conflict
# Dynatrace requires DELTA temporality for counters/histograms.
# ---------------------------------------------------------------------------
_DT_OTLP_BASE = os.getenv("TRACELOOP_BASE_URL", "")
_DT_OTLP_TOKEN = os.getenv("DT_OTLP_TOKEN", "")

# Histogram bucket boundaries per OTel GenAI semconv
_DURATION_BUCKETS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56,
                     5.12, 10.24, 20.48, 40.96, 81.92]
_TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144,
                  1048576, 4194304, 16777216, 67108864]
_TTFT_BUCKETS = [0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25,
                 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0]
_TPOT_BUCKETS = [0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5,
                 0.75, 1.0, 2.5]

if _DT_OTLP_BASE:
    _metric_exporter = OTLPMetricExporter(
        endpoint=f"{_DT_OTLP_BASE}/v1/metrics",
        headers={"Authorization": f"Api-Token {_DT_OTLP_TOKEN}"},
        preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.CUMULATIVE,
        },
    )
    _metric_reader = PeriodicExportingMetricReader(
        _metric_exporter, export_interval_millis=30_000
    )
    _meter_provider = MeterProvider(
        metric_readers=[_metric_reader],
        views=[
            View(instrument_name="gen_ai.client.operation.duration",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=_DURATION_BUCKETS)),
            View(instrument_name="gen_ai.client.token.usage",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=_TOKEN_BUCKETS)),
            View(instrument_name="gen_ai.server.time_to_first_token",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=_TTFT_BUCKETS)),
            View(instrument_name="gen_ai.server.time_per_output_token",
                 aggregation=ExplicitBucketHistogramAggregation(boundaries=_TPOT_BUCKETS)),
        ],
    )
    metrics.set_meter_provider(_meter_provider)

# ---------------------------------------------------------------------------
# OTel Logs — export Python logs to Dynatrace with trace_id correlation
# ---------------------------------------------------------------------------
if _DT_OTLP_BASE:
    _log_exporter = OTLPLogExporter(
        endpoint=f"{_DT_OTLP_BASE}/v1/logs",
        headers={"Authorization": f"Api-Token {_DT_OTLP_TOKEN}"},
    )
    _logger_provider = LoggerProvider()
    _logger_provider.add_log_record_processor(BatchLogRecordProcessor(_log_exporter))
    set_logger_provider(_logger_provider)

    _otel_handler = LoggingHandler(
        level=logging.INFO, logger_provider=_logger_provider
    )
    logging.getLogger().addHandler(_otel_handler)
    logging.getLogger().setLevel(logging.INFO)

logger = logging.getLogger("local-llm-backend")

# ---------------------------------------------------------------------------
# OpenLLMetry — traces (auto-instruments FastAPI, httpx, LLM calls)
# Must come AFTER MeterProvider so it doesn't override ours.
# ---------------------------------------------------------------------------
Traceloop.init(app_name="local-llm-backend", disable_batch=False)

_meter = metrics.get_meter("local-llm-backend", "1.0.0")

# ---------------------------------------------------------------------------
# OTel GenAI semantic convention metrics
# ---------------------------------------------------------------------------

# Required: gen_ai.client.operation.duration (histogram, seconds)
genai_client_operation_duration = _meter.create_histogram(
    name="gen_ai.client.operation.duration",
    description="GenAI operation duration",
    unit="s",
)

# Recommended: gen_ai.client.token.usage (histogram, tokens)
genai_client_token_usage = _meter.create_histogram(
    name="gen_ai.client.token.usage",
    description="Number of input and output tokens used",
    unit="{token}",
)

# Recommended: gen_ai.server.time_to_first_token (histogram, seconds)
genai_server_ttft = _meter.create_histogram(
    name="gen_ai.server.time_to_first_token",
    description="Time to generate first token",
    unit="s",
)

# Recommended: gen_ai.server.time_per_output_token (histogram, seconds)
genai_server_tpot = _meter.create_histogram(
    name="gen_ai.server.time_per_output_token",
    description="Time per output token generated after the first token",
    unit="s",
)

# ---------------------------------------------------------------------------
# Operational / custom metrics
# ---------------------------------------------------------------------------

# Total requests counter (kept from before, useful for rate)
llm_request_counter = _meter.create_counter(
    name="llm.request.count",
    description="Total LLM chat completion requests",
    unit="1",
)

# Error counter
llm_error_counter = _meter.create_counter(
    name="llm.request.errors",
    description="Total failed LLM requests",
    unit="1",
)

# Active (in-flight) requests gauge
llm_active_requests = _meter.create_up_down_counter(
    name="llm.request.active",
    description="Number of in-flight LLM requests",
    unit="1",
)

# Streaming chunks counter
llm_stream_chunks = _meter.create_counter(
    name="llm.stream.chunks",
    description="Total streaming chunks sent to clients",
    unit="1",
)

# Token throughput (tokens / second)
llm_token_throughput = _meter.create_histogram(
    name="llm.token.throughput",
    description="Output token generation throughput",
    unit="{token}/s",
)

# Request message count histogram
llm_request_message_count = _meter.create_histogram(
    name="llm.request.message_count",
    description="Number of messages in the prompt",
    unit="1",
)

app = FastAPI(title="Local LLM API Bridge", version="1.0.0")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gemma4:26b")

_parsed_ollama = urlparse(OLLAMA_BASE_URL)
_OLLAMA_HOST = _parsed_ollama.hostname or "localhost"
_OLLAMA_PORT = _parsed_ollama.port or 11434

_tracer = trace.get_tracer("local-llm-backend", "1.0.0")


def _format_input_messages(messages: list[dict]) -> str:
    """Format messages per OTel GenAI input messages JSON schema."""
    return json.dumps([
        {
            "role": m["role"],
            "parts": [{"type": "text", "content": m["content"]}],
        }
        for m in messages
    ])


def _format_output_messages(content: str, finish_reason: str = "stop") -> str:
    """Format output per OTel GenAI output messages JSON schema."""
    return json.dumps([{
        "role": "assistant",
        "parts": [{"type": "text", "content": content}],
        "finish_reason": finish_reason,
    }])


def _classify_request(messages: list[dict]) -> str:
    """Classify an Open WebUI request as user_chat, title_generation, or tag_generation."""
    last_content = (messages[-1].get("content", "") if messages else "").lower()
    if "generate a concise" in last_content and "title" in last_content:
        return "title_generation"
    if ("generate tags" in last_content or "categorize" in last_content
            or "tag the conversation" in last_content):
        return "tag_generation"
    return "user_chat"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Pydantic schemas (OpenAI-compatible)
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
# Health check
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    """Liveness probe – returns OK when the service is up."""
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# /v1/models  –  list models available in Ollama
# --------------------------------------------------------------------------- #

@app.get("/v1/models")
async def list_models():
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            ollama_models = response.json().get("models", [])
            data = [
                {
                    "id": m["name"],
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "ollama",
                }
                for m in ollama_models
            ]
            return {"object": "list", "data": data}
        except httpx.ConnectError:
            raise HTTPException(
                status_code=503,
                detail="Cannot reach Ollama. Make sure it is running on the host.",
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))


# --------------------------------------------------------------------------- #
# /v1/chat/completions  –  OpenAI-compatible chat endpoint
# --------------------------------------------------------------------------- #

@app.post("/v1/chat/completions")
@workflow(name="chat_completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    model = request.model or DEFAULT_MODEL
    start = time.time()
    _semconv_attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "server.address": _OLLAMA_HOST,
        "server.port": _OLLAMA_PORT,
    }
    logger.info(
        "Chat completion request",
        extra={"model": model, "stream": request.stream, "message_count": len(request.messages)},
    )
    llm_request_counter.add(1, {"model": model, "stream": str(request.stream)})
    llm_active_requests.add(1, {"model": model})
    llm_request_message_count.record(len(request.messages), _semconv_attrs)

    # --- OTel GenAI semantic convention attributes --------------------------
    span = trace.get_current_span()
    messages_raw = [{"role": m.role, "content": m.content} for m in request.messages]
    # Classify request type and rename span
    request_type = _classify_request(messages_raw)
    span.update_name(f"{request_type}")
    span.set_attribute("llm.request.purpose", request_type)

    # Conversation fingerprint — correlates chat + title + tag traces from the
    # same user turn by hashing the user messages (shared across all requests).
    user_msgs = [m["content"] for m in messages_raw if m["role"] == "user"]
    # Title/tag requests embed the chat history, so extract the first user msg
    if request_type != "user_chat" and user_msgs:
        # The history is embedded in the system/user prompt; hash first 200 chars
        fingerprint_input = user_msgs[0][:200]
    else:
        fingerprint_input = "|".join(user_msgs)
    conversation_fingerprint = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:12]
    span.set_attribute("conversation.fingerprint", conversation_fingerprint)

    # Authorization header forwarding for user identification
    auth_header = raw_request.headers.get("authorization", "")
    if auth_header:
        span.set_attribute("enduser.id", hashlib.sha256(auth_header.encode()).hexdigest()[:8])
    # Required
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.system", "ollama")
    span.set_attribute("gen_ai.provider.name", "ollama")
    span.set_attribute("gen_ai.request.model", model)
    # Recommended
    span.set_attribute("gen_ai.request.stream", request.stream or False)
    span.set_attribute("gen_ai.output.type", "text")
    span.set_attribute("server.address", _OLLAMA_HOST)
    span.set_attribute("server.port", _OLLAMA_PORT)
    if request.temperature is not None:
        span.set_attribute("gen_ai.request.temperature", request.temperature)
    if request.top_p is not None:
        span.set_attribute("gen_ai.request.top_p", request.top_p)
    if request.max_tokens is not None:
        span.set_attribute("gen_ai.request.max_tokens", request.max_tokens)
    # Opt-in: prompt / input messages
    span.set_attribute("gen_ai.prompt", json.dumps(messages_raw))
    span.set_attribute("gen_ai.input.messages", _format_input_messages(messages_raw))
    # Indexed prompt attributes (OpenLLMetry format — Dynatrace native mapping)
    for i, m in enumerate(messages_raw):
        span.set_attribute(f"gen_ai.prompt.{i}.role", m["role"])
        span.set_attribute(f"gen_ai.prompt.{i}.content", m["content"])
    # LLM attributes
    span.set_attribute("llm.request_type", "chat")
    span.set_attribute("llm.is_streaming", request.stream or False)

    ollama_payload: dict = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
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
            _stream_ollama(ollama_payload, model, request_type),
            media_type="text/event-stream",
        )

    result = await _non_stream_ollama(ollama_payload, model, request_type)
    duration = time.time() - start
    usage = result.get("usage", {})
    prompt_toks = usage.get("prompt_tokens", 0)
    completion_toks = usage.get("completion_tokens", 0)

    # --- OTel GenAI semconv metrics -----------------------------------------
    genai_client_operation_duration.record(duration, _semconv_attrs)
    genai_client_token_usage.record(
        prompt_toks,
        {**_semconv_attrs, "gen_ai.token.type": "input"},
    )
    genai_client_token_usage.record(
        completion_toks,
        {**_semconv_attrs, "gen_ai.token.type": "output"},
    )
    # Token throughput
    if duration > 0 and completion_toks > 0:
        llm_token_throughput.record(completion_toks / duration, _semconv_attrs)
    # Active requests
    llm_active_requests.add(-1, {"model": model})

    # --- OTel GenAI response attributes -------------------------------------
    response_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    finish_reasons = [c.get("finish_reason") for c in result.get("choices", [])]
    span.set_attribute("gen_ai.response.id", result.get("id", ""))
    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute("gen_ai.response.finish_reasons", json.dumps(finish_reasons))
    span.set_attribute("gen_ai.completion", response_content)
    span.set_attribute("gen_ai.output.messages", _format_output_messages(response_content, finish_reasons[0] if finish_reasons else "stop"))
    # Indexed completion attributes (OpenLLMetry format — Dynatrace native mapping)
    for i, choice in enumerate(result.get("choices", [])):
        span.set_attribute(f"gen_ai.completion.{i}.finish_reason", choice.get("finish_reason", "stop"))
        span.set_attribute(f"gen_ai.completion.{i}.content", choice.get("message", {}).get("content", ""))
    span.set_attribute("gen_ai.usage.input_tokens", prompt_toks)
    span.set_attribute("gen_ai.usage.output_tokens", completion_toks)
    span.set_attribute("gen_ai.usage.prompt_tokens", prompt_toks)
    span.set_attribute("gen_ai.usage.completion_tokens", completion_toks)
    span.set_attribute("gen_ai.usage.total_tokens", usage.get("total_tokens", 0))
    logger.info(
        "Chat completion finished",
        extra={
            "model": model,
            "duration_s": round(duration, 3),
            "prompt_tokens": prompt_toks,
            "completion_tokens": completion_toks,
            "total_tokens": usage.get("total_tokens", 0),
        },
    )
    return result


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

@workflow(name="stream_ollama")
async def _stream_ollama(payload: dict, model: str, request_type: str = "user_chat"):
    """Translate Ollama NDJSON stream → OpenAI SSE stream."""
    logger.info("Starting streaming response", extra={"model": model, "request_type": request_type})
    stream_start = time.time()
    first_token_time: float | None = None
    _semconv_attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "server.address": _OLLAMA_HOST,
        "server.port": _OLLAMA_PORT,
    }
    span = trace.get_current_span()
    span.update_name(f"{request_type} stream")
    span.set_attribute("llm.request.purpose", request_type)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.system", "ollama")
    span.set_attribute("gen_ai.provider.name", "ollama")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.output.type", "text")
    span.set_attribute("server.address", _OLLAMA_HOST)
    span.set_attribute("server.port", _OLLAMA_PORT)
    # LLM + indexed prompt attributes (OpenLLMetry format — Dynatrace native mapping)
    span.set_attribute("llm.request_type", "chat")
    span.set_attribute("llm.is_streaming", True)
    for i, m in enumerate(payload.get("messages", [])):
        span.set_attribute(f"gen_ai.prompt.{i}.role", m["role"])
        span.set_attribute(f"gen_ai.prompt.{i}.content", m["content"])
    full_response_parts: list[str] = []
    chunk_count = 0
    async with httpx.AsyncClient(timeout=300.0) as client:
        with _tracer.start_as_current_span(
            "POST", kind=trace.SpanKind.CLIENT,
            attributes={
                "http.request.method": "POST",
                "url.full": f"{OLLAMA_BASE_URL}/api/chat",
                "server.address": _OLLAMA_HOST,
                "server.port": _OLLAMA_PORT,
            },
        ):
            async with client.stream(
                "POST", f"{OLLAMA_BASE_URL}/api/chat", json=payload
            ) as response:
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
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": content} if content else {},
                                "finish_reason": "stop" if done else None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"

                    if done:
                        stream_duration = time.time() - stream_start
                        prompt_toks = chunk.get("prompt_eval_count", 0)
                        completion_toks = chunk.get("eval_count", 0)
                        full_text = "".join(full_response_parts)

                        # Span attributes
                        span.set_attribute("gen_ai.response.finish_reasons", json.dumps(["stop"]))
                        span.set_attribute("gen_ai.completion", full_text)
                        span.set_attribute("gen_ai.output.messages", _format_output_messages(full_text))
                        # Indexed completion attributes (Dynatrace native mapping)
                        span.set_attribute("gen_ai.completion.0.content", full_text)
                        span.set_attribute("gen_ai.completion.0.finish_reason", "stop")
                        span.set_attribute("gen_ai.usage.input_tokens", prompt_toks)
                        span.set_attribute("gen_ai.usage.output_tokens", completion_toks)
                        span.set_attribute("gen_ai.usage.prompt_tokens", prompt_toks)
                        span.set_attribute("gen_ai.usage.completion_tokens", completion_toks)
                        span.set_attribute("gen_ai.usage.total_tokens", prompt_toks + completion_toks)

                        # GenAI semconv metrics
                        genai_client_operation_duration.record(stream_duration, _semconv_attrs)
                        genai_client_token_usage.record(
                            prompt_toks, {**_semconv_attrs, "gen_ai.token.type": "input"})
                        genai_client_token_usage.record(
                            completion_toks, {**_semconv_attrs, "gen_ai.token.type": "output"})

                        # Time-to-first-token
                        if first_token_time is not None:
                            ttft = first_token_time - stream_start
                            genai_server_ttft.record(ttft, _semconv_attrs)

                        # Time-per-output-token (after first token)
                        if first_token_time is not None and completion_toks > 1:
                            decode_time = time.time() - first_token_time
                            tpot = decode_time / (completion_toks - 1)
                            genai_server_tpot.record(tpot, _semconv_attrs)

                        # Operational metrics
                        llm_stream_chunks.add(chunk_count, {"model": model})
                        if stream_duration > 0 and completion_toks > 0:
                            llm_token_throughput.record(
                                completion_toks / stream_duration, _semconv_attrs)
                        llm_active_requests.add(-1, {"model": model})

                        yield "data: [DONE]\n\n"
                        break


@workflow(name="non_stream_ollama")
async def _non_stream_ollama(payload: dict, model: str, request_type: str = "user_chat") -> dict:
    """Call Ollama without streaming and return an OpenAI-compatible response."""
    ollama_start = time.time()
    _ns_attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "server.address": _OLLAMA_HOST,
        "server.port": _OLLAMA_PORT,
    }
    span = trace.get_current_span()
    span.update_name(f"{request_type} non-stream")
    span.set_attribute("llm.request.purpose", request_type)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.system", "ollama")
    span.set_attribute("gen_ai.provider.name", "ollama")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("server.address", _OLLAMA_HOST)
    span.set_attribute("server.port", _OLLAMA_PORT)
    # LLM + indexed prompt attributes (OpenLLMetry format — Dynatrace native mapping)
    span.set_attribute("llm.request_type", "chat")
    span.set_attribute("llm.is_streaming", False)
    for i, m in enumerate(payload.get("messages", [])):
        span.set_attribute(f"gen_ai.prompt.{i}.role", m["role"])
        span.set_attribute(f"gen_ai.prompt.{i}.content", m["content"])
    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            with _tracer.start_as_current_span(
                "POST", kind=trace.SpanKind.CLIENT,
                attributes={
                    "http.request.method": "POST",
                    "url.full": f"{OLLAMA_BASE_URL}/api/chat",
                    "server.address": _OLLAMA_HOST,
                    "server.port": _OLLAMA_PORT,
                },
            ):
                response = await client.post(
                    f"{OLLAMA_BASE_URL}/api/chat", json=payload
                )
                response.raise_for_status()
        except httpx.ConnectError:
            logger.error("Cannot reach Ollama at %s", OLLAMA_BASE_URL)
            span.set_attribute("error.type", "ConnectError")
            llm_error_counter.add(1, {**_ns_attrs, "error.type": "ConnectError"})
            llm_active_requests.add(-1, {"model": model})
            raise HTTPException(
                status_code=503,
                detail="Cannot reach Ollama. Make sure it is running on the host.",
            )

    data = response.json()
    content = data.get("message", {}).get("content", "")
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)

    # Ollama timing data (nanoseconds) for TTFT / TPOT
    prompt_eval_duration = data.get("prompt_eval_duration", 0)
    eval_duration = data.get("eval_duration", 0)

    if prompt_eval_duration > 0:
        genai_server_ttft.record(prompt_eval_duration / 1e9, _ns_attrs)
    if eval_duration > 0 and completion_tokens > 1:
        genai_server_tpot.record(
            (eval_duration / 1e9) / (completion_tokens - 1), _ns_attrs)

    ollama_duration = time.time() - ollama_start
    if ollama_duration > 0 and completion_tokens > 0:
        llm_token_throughput.record(completion_tokens / ollama_duration, _ns_attrs)

    # Response attributes on the non_stream_ollama span
    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute("gen_ai.completion", content)
    span.set_attribute("gen_ai.output.messages", _format_output_messages(content))
    span.set_attribute("gen_ai.response.finish_reasons", json.dumps(["stop"]))
    span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
    # Indexed completion attributes (Dynatrace native mapping)
    span.set_attribute("gen_ai.completion.0.content", content)
    span.set_attribute("gen_ai.completion.0.finish_reason", "stop")

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
