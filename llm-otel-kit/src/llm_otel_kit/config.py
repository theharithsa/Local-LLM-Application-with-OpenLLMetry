"""Configuration models for llm-otel-kit."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    """LLM provider connection settings.

    Attributes:
        name: Provider identifier — ``"ollama"``, ``"openai"``, or ``"anthropic"``.
        base_url: API base URL (e.g. ``http://localhost:11434``,
                  ``https://api.openai.com``).
        api_key: API key for cloud providers.  Leave empty for local providers.
        default_model: Fallback model when the request doesn't specify one.
    """

    name: str = "ollama"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    default_model: str = ""


@dataclass
class AppConfig:
    """Full application configuration — provider + observability.

    Attributes:
        app_name: OTel service name.
        provider: LLM provider settings.
        otlp_endpoint: OTLP base URL (e.g. Dynatrace OTLP endpoint).
        otlp_token: Auth token for the OTLP exporter.
    """

    app_name: str = "llm-backend"
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    otlp_endpoint: str = ""
    otlp_token: str = ""

    @classmethod
    def from_env(cls) -> AppConfig:
        """Build config from environment variables.

        Env vars:
            ``APP_NAME``              — OTel service name (default: ``llm-backend``)
            ``LLM_PROVIDER``          — ``ollama`` | ``openai`` | ``anthropic``
            ``LLM_BASE_URL``          — Provider API base URL
            ``LLM_API_KEY``           — API key for cloud providers
            ``DEFAULT_MODEL``         — Fallback model name
            ``TRACELOOP_BASE_URL``    — OTLP endpoint
            ``DT_OTLP_TOKEN``         — Dynatrace API token

        Legacy env vars (``OLLAMA_BASE_URL``) are supported as fallbacks.
        """
        provider_name = os.getenv("LLM_PROVIDER", "ollama").lower()

        # Resolve base URL with legacy fallback
        base_url = os.getenv("LLM_BASE_URL", "")
        if not base_url:
            if provider_name == "ollama":
                base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
            elif provider_name == "openai":
                base_url = "https://api.openai.com"
            elif provider_name == "anthropic":
                base_url = "https://api.anthropic.com"

        return cls(
            app_name=os.getenv("APP_NAME", "llm-backend"),
            provider=ProviderConfig(
                name=provider_name,
                base_url=base_url,
                api_key=os.getenv("LLM_API_KEY", ""),
                default_model=os.getenv("DEFAULT_MODEL", ""),
            ),
            otlp_endpoint=os.getenv("TRACELOOP_BASE_URL", ""),
            otlp_token=os.getenv("DT_OTLP_TOKEN", ""),
        )
