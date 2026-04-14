"""Provider registry — factory for LLM backend providers."""

from llm_otel_kit.config import ProviderConfig
from llm_otel_kit.providers.base import LLMProvider


def create_provider(config: ProviderConfig) -> LLMProvider:
    """Instantiate the correct provider from config.

    Supported providers:
        - ``ollama`` — Ollama native API (``/api/chat``)
        - ``openai``  — OpenAI-compatible (works with OpenAI, vLLM, llama.cpp,
          LM Studio, Groq, Together, Fireworks, Azure OpenAI, LiteLLM, etc.)
        - ``anthropic`` — Anthropic Messages API (``/v1/messages``)
    """
    name = config.name.lower()

    if name == "ollama":
        from llm_otel_kit.providers.ollama import OllamaProvider
        return OllamaProvider(config)
    if name in ("openai", "vllm", "llamacpp", "lmstudio", "groq", "together",
                "fireworks", "azure_openai", "litellm"):
        from llm_otel_kit.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(config)
    if name == "anthropic":
        from llm_otel_kit.providers.anthropic import AnthropicProvider
        return AnthropicProvider(config)

    raise ValueError(
        f"Unknown provider '{config.name}'. "
        "Supported: ollama, openai, anthropic, vllm, llamacpp, lmstudio, "
        "groq, together, fireworks, azure_openai, litellm"
    )


__all__ = ["LLMProvider", "create_provider"]
