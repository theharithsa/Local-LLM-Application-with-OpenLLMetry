# llm-otel-kit

Drop-in OpenTelemetry GenAI observability for any LLM backend — local or cloud.

## What it does

`llm-otel-kit` gives you **full OTel GenAI semantic convention coverage** for any LLM provider in ~10 lines of code:

- **Traces** with `gen_ai.*` span attributes (model, tokens, latency, streaming mode)
- **Metrics** — 10 instruments: operation duration, token usage, TTFT, TPOT, throughput, error rate, active requests
- **Logs** exported via OTLP with structured context (model, duration, token counts)
- **Dynatrace-ready** — correct temporality (DELTA for counters/histograms, CUMULATIVE for UpDownCounters)

## Supported Providers

| Provider | Type | Config name |
|----------|------|-------------|
| Ollama | Local | `ollama` |
| OpenAI | Cloud | `openai` |
| Anthropic | Cloud | `anthropic` |
| vLLM | Local | `vllm` |
| llama.cpp | Local | `llamacpp` |
| LM Studio | Local | `lmstudio` |
| Groq | Cloud | `groq` |
| Together | Cloud | `together` |
| Fireworks | Cloud | `fireworks` |
| Azure OpenAI | Cloud | `azure_openai` |
| LiteLLM | Proxy | `litellm` |

## Quick Start

```python
from llm_otel_kit import AppConfig, GenAIMetrics, init_observability, create_provider

config = AppConfig.from_env()
otel = init_observability(config.app_name, config.otlp_endpoint, config.otlp_token)
provider = create_provider(config.provider)
m = GenAIMetrics(otel.meter)

# Use provider.complete() / provider.stream() for instrumented LLM calls
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `ollama` | Provider name (see table above) |
| `LLM_BASE_URL` | `http://localhost:11434` | Provider API base URL |
| `LLM_API_KEY` | (empty) | API key for cloud providers |
| `DEFAULT_MODEL` | (empty) | Fallback model name |
| `APP_NAME` | `llm-backend` | OTel service name |
| `TRACELOOP_BASE_URL` | (empty) | OTLP endpoint URL |
| `DT_OTLP_TOKEN` | (empty) | Dynatrace API token |

## Install

```bash
pip install llm-otel-kit
```

For Anthropic support:

```bash
pip install llm-otel-kit[anthropic]
```

## License

MIT
