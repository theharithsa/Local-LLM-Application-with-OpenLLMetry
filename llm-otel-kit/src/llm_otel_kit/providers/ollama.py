"""Ollama native API provider (``/api/chat``)."""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from llm_otel_kit.providers.base import (
    CompletionResult,
    LLMProvider,
    StreamChunk,
    TimingInfo,
)


class OllamaProvider(LLMProvider):
    """Provider for Ollama running locally or on a remote host."""

    @property
    def system_name(self) -> str:
        return "ollama"

    def build_payload(
        self,
        model: str,
        messages: list[dict],
        stream: bool,
        **kwargs,
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": {},
        }
        if kwargs.get("temperature") is not None:
            payload["options"]["temperature"] = kwargs["temperature"]
        if kwargs.get("top_p") is not None:
            payload["options"]["top_p"] = kwargs["top_p"]
        if kwargs.get("max_tokens") is not None:
            payload["options"]["num_predict"] = kwargs["max_tokens"]
        return payload

    async def complete(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> CompletionResult:
        response = await client.post(f"{self.base_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()

        prompt_eval_ns = data.get("prompt_eval_duration", 0)
        eval_ns = data.get("eval_duration", 0)
        completion_tokens = data.get("eval_count", 0)

        ttft = (prompt_eval_ns / 1e9) if prompt_eval_ns > 0 else None
        tpot = None
        if eval_ns > 0 and completion_tokens > 1:
            tpot = (eval_ns / 1e9) / (completion_tokens - 1)

        return CompletionResult(
            content=data.get("message", {}).get("content", ""),
            model=data.get("model", payload.get("model", "")),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=completion_tokens,
            finish_reason="stop",
            timing=TimingInfo(ttft=ttft, tpot=tpot),
        )

    async def stream(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> AsyncIterator[StreamChunk]:
        async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                content = chunk.get("message", {}).get("content", "")
                done = chunk.get("done", False)

                sc = StreamChunk(content=content, done=done)
                if done:
                    prompt_eval_ns = chunk.get("prompt_eval_duration", 0)
                    eval_ns = chunk.get("eval_duration", 0)
                    sc.prompt_tokens = chunk.get("prompt_eval_count", 0)
                    sc.completion_tokens = chunk.get("eval_count", 0)
                    sc.finish_reason = "stop"
                    sc.timing = TimingInfo(
                        ttft=(prompt_eval_ns / 1e9) if prompt_eval_ns > 0 else None,
                        tpot=((eval_ns / 1e9) / (sc.completion_tokens - 1)
                              if eval_ns > 0 and sc.completion_tokens > 1 else None),
                    )
                yield sc

    async def list_models(self, client: httpx.AsyncClient) -> list[dict]:
        response = await client.get(f"{self.base_url}/api/tags")
        response.raise_for_status()
        return [
            {"id": m["name"], "object": "model",
             "created": int(time.time()), "owned_by": "ollama"}
            for m in response.json().get("models", [])
        ]
