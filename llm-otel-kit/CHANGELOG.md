# Changelog

## 0.1.0 (2025-07-12)

### Added

- Initial release
- Provider abstraction: `LLMProvider` ABC with `complete()`, `stream()`, `list_models()`
- Providers: Ollama, OpenAI-compatible (OpenAI, vLLM, llama.cpp, LM Studio, Groq, Together, Fireworks, Azure OpenAI, LiteLLM), Anthropic
- OTel bootstrap: `init_observability()` with Dynatrace-compatible temporality
- GenAI metrics: 10 instruments following OTel GenAI semantic conventions
- Span helpers: `set_genai_span()`, `set_genai_response()`, `classify_request()`
- Config: `AppConfig.from_env()` with legacy `OLLAMA_BASE_URL` fallback
