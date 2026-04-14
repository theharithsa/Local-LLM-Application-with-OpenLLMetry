"""
{{ cookiecutter.project_name }} — LLM API Bridge with full GenAI observability.

Provider-agnostic FastAPI backend powered by llm-otel-kit.
"""

import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from traceloop.sdk.decorators import workflow
from opentelemetry import trace

from llm_otel_kit import (
    AppConfig,
    GenAIMetrics,
    classify_request,
    create_provider,
    init_observability,
    record_metrics,
    set_genai_response,
    set_genai_span,
)
from llm_otel_kit.spans import semconv_attrs

# ---------------------------------------------------------------------------
# Config + bootstrap
# ---------------------------------------------------------------------------
config = AppConfig.from_env()
otel = init_observability(config.app_name, config.otlp_endpoint, config.otlp_token)
provider = create_provider(config.provider)

logger = otel.logger
m = GenAIMetrics(otel.meter)
_tracer = otel.tracer

app = FastAPI(title="{{ cookiecutter.project_name }} API", version="1.0.0")

DEFAULT_MODEL = config.provider.default_model

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "provider": provider.system_name}


@app.get("/v1/models")
async def list_models():
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            return {"object": "list", "data": await provider.list_models(client)}
        except httpx.ConnectError:
            raise HTTPException(503, f"Cannot reach {provider.system_name} at {provider.base_url}")
        except Exception as exc:
            raise HTTPException(503, str(exc))


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_request: Request):
    model = request.model or DEFAULT_MODEL
    messages_raw = [{"role": msg.role, "content": msg.content} for msg in request.messages]

    m.request_count.add(1, {"model": model, "stream": str(request.stream)})
    m.active_requests.add(1, {"model": model})

    payload = provider.build_payload(
        model=model,
        messages=messages_raw,
        stream=bool(request.stream),
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
    )
    extra_ctx = {
        "auth_header": raw_request.headers.get("authorization", ""),
        "temperature": request.temperature,
        "top_p": request.top_p,
        "max_tokens": request.max_tokens,
    }

    if request.stream:
        return StreamingResponse(
            _stream_llm(payload, model, messages_raw, extra_ctx),
            media_type="text/event-stream",
        )
    return await _non_stream_llm(payload, model, messages_raw, extra_ctx)


# ---------------------------------------------------------------------------
# gen_ai.chat span — streaming
# ---------------------------------------------------------------------------

@workflow(name="gen_ai.chat")
async def _stream_llm(payload: dict, model: str,
                      messages: list[dict], extra_ctx: dict):
    stream_start = time.time()
    first_token_time: float | None = None
    request_type = classify_request(messages)
    attrs = semconv_attrs(model, provider.host, provider.port)
    span = trace.get_current_span()

    set_genai_span(span, model, request_type, True, messages,
                   provider.host, provider.port, **extra_ctx)
    m.message_count.record(len(messages), attrs)
    logger.info("Streaming start", extra={"model": model, "request_type": request_type})

    full_response_parts: list[str] = []
    chunk_count = 0

    async with httpx.AsyncClient(timeout=300.0) as client:
        with _tracer.start_as_current_span(
            f"POST {provider.host}:{provider.port}",
            kind=trace.SpanKind.CLIENT,
            attributes={
                "http.request.method": "POST",
                "url.full": provider.base_url,
                "server.address": provider.host,
                "server.port": provider.port,
            },
        ) as http_span:
            try:
                async for sc in provider.stream(client, payload):
                    if sc.content:
                        full_response_parts.append(sc.content)
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
                            "delta": {"content": sc.content} if sc.content else {},
                            "finish_reason": "stop" if sc.done else None,
                        }],
                    }
                    yield f"data: {json.dumps(openai_chunk)}\n\n"

                    if sc.done:
                        duration = time.time() - stream_start
                        full_response = "".join(full_response_parts)

                        set_genai_response(span, full_response, model,
                                           sc.prompt_tokens, sc.completion_tokens)
                        http_span.set_attribute("http.response.status_code", 200)

                        ttft = sc.timing.ttft if sc.timing.ttft else (
                            (first_token_time - stream_start) if first_token_time else None)
                        tpot = sc.timing.tpot if sc.timing.tpot else (
                            ((time.time() - first_token_time) / (sc.completion_tokens - 1))
                            if first_token_time and sc.completion_tokens > 1 else None)

                        record_metrics(m, attrs, model, duration,
                                       sc.prompt_tokens, sc.completion_tokens, ttft, tpot)
                        m.stream_chunks.add(chunk_count, {"model": model})

                        logger.info("Stream done", extra={
                            "model": model, "duration_s": round(duration, 3),
                            "prompt_tokens": sc.prompt_tokens,
                            "completion_tokens": sc.completion_tokens,
                        })
                        yield "data: [DONE]\n\n"
                        break

            except httpx.ConnectError:
                logger.error("Cannot reach %s at %s", provider.system_name, provider.base_url)
                span.set_attribute("error.type", "ConnectError")
                m.error_count.add(1, {**attrs, "error.type": "ConnectError"})
                m.active_requests.add(-1, {"model": model})
                raise HTTPException(503, f"Cannot reach {provider.system_name}.")


# ---------------------------------------------------------------------------
# gen_ai.chat span — non-streaming
# ---------------------------------------------------------------------------

@workflow(name="gen_ai.chat")
async def _non_stream_llm(payload: dict, model: str,
                          messages: list[dict], extra_ctx: dict) -> dict:
    start = time.time()
    request_type = classify_request(messages)
    attrs = semconv_attrs(model, provider.host, provider.port)
    span = trace.get_current_span()

    set_genai_span(span, model, request_type, False, messages,
                   provider.host, provider.port, **extra_ctx)
    m.message_count.record(len(messages), attrs)

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            with _tracer.start_as_current_span(
                f"POST {provider.host}:{provider.port}",
                kind=trace.SpanKind.CLIENT,
                attributes={
                    "http.request.method": "POST",
                    "url.full": provider.base_url,
                    "server.address": provider.host,
                    "server.port": provider.port,
                },
            ) as http_span:
                result = await provider.complete(client, payload)
                http_span.set_attribute("http.response.status_code", 200)
        except httpx.ConnectError:
            logger.error("Cannot reach %s at %s", provider.system_name, provider.base_url)
            span.set_attribute("error.type", "ConnectError")
            m.error_count.add(1, {**attrs, "error.type": "ConnectError"})
            m.active_requests.add(-1, {"model": model})
            raise HTTPException(503, f"Cannot reach {provider.system_name}.")

    resp_id = result.response_id or f"chatcmpl-{uuid.uuid4().hex[:8]}"
    set_genai_response(span, result.content, model,
                       result.prompt_tokens, result.completion_tokens,
                       result.finish_reason, resp_id)

    duration = time.time() - start
    record_metrics(m, attrs, model, duration,
                   result.prompt_tokens, result.completion_tokens,
                   result.timing.ttft, result.timing.tpot)

    logger.info("Chat done", extra={
        "model": model, "duration_s": round(duration, 3),
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    })

    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.content},
            "finish_reason": result.finish_reason,
        }],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.prompt_tokens + result.completion_tokens,
        },
    }
