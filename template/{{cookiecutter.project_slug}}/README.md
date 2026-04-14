# {{ cookiecutter.project_name }}

LLM application with full OpenTelemetry GenAI observability, powered by [llm-otel-kit](https://github.com/theharithsa/Local-LLM-Application-with-OpenLLMetry).

## Architecture

```
Open WebUI → FastAPI Backend (llm-otel-kit) → {{ cookiecutter.llm_provider }} LLM
                    ↓
             OTel Collector
                    ↓
{%- if cookiecutter.observability_backend == 'dynatrace' %}
              Dynatrace
{%- elif cookiecutter.observability_backend == 'jaeger' %}
               Jaeger
{%- else %}
          (no exporter)
{%- endif %}
```

## Quick Start

1. Copy `.env.example` to `.env` and fill in your values:
   ```bash
   cp .env.example .env
   ```

2. Start all services:
   ```bash
   docker compose up -d
   ```

3. Open the chat UI at [http://localhost:{{ cookiecutter.frontend_port }}](http://localhost:{{ cookiecutter.frontend_port }})

## Provider: {{ cookiecutter.llm_provider }}

| Setting | Value |
|---------|-------|
| Provider | `{{ cookiecutter.llm_provider }}` |
| Base URL | `{{ cookiecutter.llm_base_url }}` |
| Default Model | `{{ cookiecutter.default_model }}` |
| Backend Port | `{{ cookiecutter.backend_port }}` |
| Frontend Port | `{{ cookiecutter.frontend_port }}` |

## Observability

{%- if cookiecutter.observability_backend == 'dynatrace' %}
This project exports traces, metrics, and logs to **Dynatrace** via OTLP/HTTP.
Set `TRACELOOP_BASE_URL` and `DT_OTLP_TOKEN` in your `.env` file.
{%- elif cookiecutter.observability_backend == 'jaeger' %}
This project exports traces to **Jaeger**. View traces at [http://localhost:16686](http://localhost:16686).
{%- else %}
No observability backend is configured. Add one by setting `TRACELOOP_BASE_URL` in your `.env`.
{%- endif %}

Generated with [cookiecutter](https://cookiecutter-pypackage.readthedocs.io/) from the Local LLM App template.
