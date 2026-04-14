"""Abstract base class for LLM providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from llm_otel_kit.config import ProviderConfig


@dataclass
class TimingInfo:
    """TTFT / TPOT extracted from the provider response."""
    ttft: float | None = None
    tpot: float | None = None


@dataclass
class CompletionResult:
    """Normalised result of a non-streaming chat completion."""
    content: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = "stop"
    response_id: str = ""
    timing: TimingInfo = field(default_factory=TimingInfo)


@dataclass
class StreamChunk:
    """One chunk from a streaming completion."""
    content: str = ""
    done: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    timing: TimingInfo = field(default_factory=TimingInfo)


class LLMProvider(ABC):
    """Interface that every LLM backend must implement."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)

    @property
    @abstractmethod
    def system_name(self) -> str:
        """OTel ``gen_ai.system`` value (e.g. ``"ollama"``, ``"openai"``)."""

    @abstractmethod
    def build_payload(
        self,
        model: str,
        messages: list[dict],
        stream: bool,
        **kwargs,
    ) -> dict:
        """Translate OpenAI-format request into provider-native payload."""

    @abstractmethod
    async def complete(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> CompletionResult:
        """Non-streaming chat completion."""

    @abstractmethod
    async def stream(
        self,
        client: httpx.AsyncClient,
        payload: dict,
    ) -> AsyncIterator[StreamChunk]:
        """Streaming chat completion — yields ``StreamChunk``s."""

    @abstractmethod
    async def list_models(self, client: httpx.AsyncClient) -> list[dict]:
        """Return models in OpenAI list format ``[{"id": ..., "object": "model", ...}]``."""
