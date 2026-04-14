"""Anthropic Messages API provider (``/v1/messages``)."""

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


class AnthropicProvider(LLMProvider):
    """Provider for the Anthropic Claude API."""

    @property
    def system_name(self) -> str:
        return "anthropic"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
        }

    def build_payload(
        self,
        model: str,
        messages: list[dict],
        stream: bool,
        **kwargs,
    ) -> dict:
        # Anthropic separates system message from the messages array
        system_parts: list[str] = []
        user_messages: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                system_parts.append(msg["content"])
            else:
                user_messages.append({"role": msg["role"], "content": msg["content"]})

        payload: dict = {
            "model": model,
            "messages": user_messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]
        return payload

    async def complete(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> CompletionResult:
        response = await client.post(
            f"{self.base_url}/v1/messages",
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        data = response.json()

        content_blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        usage = data.get("usage", {})

        return CompletionResult(
            content=text,
            model=data.get("model", payload.get("model", "")),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            finish_reason=data.get("stop_reason", "end_turn"),
            response_id=data.get("id", ""),
            timing=TimingInfo(),
        )

    async def stream(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> AsyncIterator[StreamChunk]:
        prompt_tokens = 0
        completion_tokens = 0

        async with client.stream(
            "POST",
            f"{self.base_url}/v1/messages",
            json=payload,
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "message_start":
                    usage = event.get("message", {}).get("usage", {})
                    prompt_tokens = usage.get("input_tokens", 0)

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    yield StreamChunk(content=delta.get("text", ""))

                elif event_type == "message_delta":
                    usage = event.get("usage", {})
                    completion_tokens = usage.get("output_tokens", 0)
                    stop_reason = event.get("delta", {}).get("stop_reason", "end_turn")
                    yield StreamChunk(
                        done=True,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        finish_reason=stop_reason,
                        timing=TimingInfo(),
                    )

    async def list_models(self, client: httpx.AsyncClient) -> list[dict]:
        # Anthropic doesn't have a models endpoint; return a static list
        models = ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]
        return [
            {"id": m, "object": "model",
             "created": int(time.time()), "owned_by": "anthropic"}
            for m in models
        ]
