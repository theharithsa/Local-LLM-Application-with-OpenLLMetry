"""OpenAI-compatible API provider.

Works with: OpenAI, Azure OpenAI, vLLM, llama.cpp (server mode),
LM Studio, Groq, Together.ai, Fireworks.ai, LiteLLM, LocalAI, etc.
"""

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


class OpenAICompatProvider(LLMProvider):
    """Provider for any OpenAI-compatible API endpoint."""

    @property
    def system_name(self) -> str:
        return self.config.name if self.config.name != "openai" else "openai"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        return h

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
        }
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def complete(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> CompletionResult:
        response = await client.post(
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        usage = data.get("usage", {})

        return CompletionResult(
            content=choice.get("message", {}).get("content", ""),
            model=data.get("model", payload.get("model", "")),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            response_id=data.get("id", ""),
            timing=TimingInfo(),  # OpenAI API doesn't expose server-side timing
        )

    async def stream(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> AsyncIterator[StreamChunk]:
        async with client.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                content = delta.get("content", "")
                finish_reason = choice.get("finish_reason")

                # Usage arrives in the final chunk when stream_options.include_usage is set
                usage = chunk.get("usage") or {}

                sc = StreamChunk(
                    content=content,
                    done=finish_reason is not None,
                    finish_reason=finish_reason or "",
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    timing=TimingInfo(),
                )
                yield sc

    async def list_models(self, client: httpx.AsyncClient) -> list[dict]:
        response = await client.get(
            f"{self.base_url}/v1/models",
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"id": m["id"], "object": "model",
             "created": m.get("created", int(time.time())),
             "owned_by": m.get("owned_by", self.system_name)}
            for m in data.get("data", [])
        ]
