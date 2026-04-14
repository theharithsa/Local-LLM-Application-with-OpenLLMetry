"""llm-otel-kit — Drop-in OTel GenAI observability for any LLM backend."""

from llm_otel_kit.bootstrap import OTelComponents, init_observability
from llm_otel_kit.config import AppConfig, ProviderConfig
from llm_otel_kit.metrics import GenAIMetrics
from llm_otel_kit.providers import create_provider
from llm_otel_kit.spans import (
    classify_request,
    detect_provider,
    record_metrics,
    set_genai_response,
    set_genai_span,
)

__all__ = [
    "AppConfig",
    "GenAIMetrics",
    "OTelComponents",
    "ProviderConfig",
    "classify_request",
    "create_provider",
    "detect_provider",
    "init_observability",
    "record_metrics",
    "set_genai_response",
    "set_genai_span",
]
