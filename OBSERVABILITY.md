# Observability Setup — Step by Step

This document describes everything implemented to achieve full-stack observability for the Local LLM App, exporting traces, metrics, logs, and RUM data to Dynatrace.

---

## Architecture Overview

```
┌──────────────┐      ┌─────────────────┐      ┌──────────────┐
│  Open WebUI  │─────▶│  FastAPI Backend │─────▶│    Ollama     │
│  (Frontend)  │      │  (Proxy + OTel)  │      │  (Host LLM)  │
│  port 3000   │      │   port 8000      │      │  port 11434   │
└──────┬───────┘      └────────┬─────────┘      └──────────────┘
       │                       │
       │ OTel gRPC             │ OTLP/HTTP
       ▼                       ▼
┌──────────────┐      ┌──────────────────────────────────────────┐
│ OTel Collector│─────▶│           Dynatrace Platform             │
│  (gRPC→HTTP) │      │  (Traces, Metrics, Logs, RUM, Dashboard) │
└──────────────┘      └──────────────────────────────────────────┘
       ▲
       │ Dynatrace RUM JS (injected via entrypoint.sh)
       │
  Browser ──────────────────────────────────────────────────────▶ Dynatrace
```

**Three-tier flow**: `Open WebUI → FastAPI Backend → Ollama`. No shortcuts.

Open WebUI **never** connects to Ollama directly — all LLM traffic routes through the instrumented FastAPI backend (`ENABLE_OLLAMA_API=false`).

**Services** (via `docker-compose.yml`):
| Service | Image / Build | Purpose |
|---------|--------------|---------|
| `backend` | `./backend` (Dockerfile) | OpenAI-compatible proxy with full OTel GenAI instrumentation |
| `otel-collector` | `otel/opentelemetry-collector-contrib:latest` | gRPC→HTTP protocol bridge for Open WebUI traces |
| `open-webui` | `ghcr.io/open-webui/open-webui:main` | Chat frontend with native OTel + Dynatrace RUM |

---

## Step 1 — FastAPI Backend as OpenAI-Compatible Proxy

**What**: A Python FastAPI app that translates OpenAI API format to Ollama's API format.

**Why**: Open WebUI speaks OpenAI API. Ollama has its own format. The proxy bridges them and gives us a single instrumentation point.

**Key endpoints**:
- `POST /v1/chat/completions` → translates to Ollama `POST /api/chat`
- `GET /v1/models` → translates to Ollama `GET /api/tags`

**Files**: `backend/main.py`, `backend/otel_setup.py`, `backend/Dockerfile`, `backend/requirements.txt`

---

## Step 2 — Reusable OTel Bootstrap (`otel_setup.py`)

**What**: Extracted all OpenTelemetry initialization into a reusable module (`backend/otel_setup.py`) that bootstraps metrics, logs, and tracing in the correct order for Dynatrace compatibility.

**Key exports**:
- `init_observability(app_name)` → returns `OTelComponents(meter, tracer, logger)`
- `GenAIMetrics(meter)` → dataclass with all 10 metric instruments pre-created

**Init order** (critical):
1. Create `MeterProvider` with DELTA temporality + GenAI histogram bucket Views
2. Set global `MeterProvider`
3. Create `LoggerProvider` with OTLP exporter
4. Call `Traceloop.init()` — must come AFTER MeterProvider to avoid conflicts

```python
from otel_setup import init_observability, GenAIMetrics

otel = init_observability("local-llm-backend")
m = GenAIMetrics(otel.meter)
```

---

## Step 3 — Distributed Traces (OpenLLMetry / Traceloop SDK)

**What**: Integrated [OpenLLMetry](https://github.com/traceloop/openllmetry) (Traceloop SDK) to automatically instrument the backend and generate OpenTelemetry traces.

**How**:
1. `traceloop-sdk>=0.59.0` in `requirements.txt`
2. Traceloop initialized via `otel_setup.py` with `disable_batch=False`
3. Root span decorated with `@workflow(name="gen_ai.chat")`
4. Manual child spans via `_tracer.start_as_current_span("POST /api/chat")` for the outbound Ollama call

**Span structure per request**:
```
gen_ai.chat (root — request type + model in span name)
  └── POST /api/chat (child — httpx call to Ollama)
```

**Request classification**: Each request is classified by `_classify_request()` into one of:
- `User Chat` — actual user conversation
- `Title Generation` — Open WebUI auto-title
- `Tag Generation` — Open WebUI auto-tagging
- `Suggestion Generation` — follow-up suggestions
- `System Prompt` — system-only messages

The classification is stored as `llm.request.purpose` on the span and used in the span name (e.g., `"User Chat · gemma4:latest"`).

**Provider detection**: `_detect_provider()` maps model name prefixes to providers (OpenAI, Anthropic, Google, Mistral, etc.), defaulting to `"ollama"` for local models.

---

## Step 4 — GenAI Span Attributes (OTel Semantic Conventions)

**What**: Rich span attributes following the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) so traces contain the full context of each LLM call.

**Request attributes** (set by `_set_genai_span()`):

| Attribute | Value | Description |
|-----------|-------|-------------|
| `gen_ai.system` | auto-detected | LLM provider (ollama, openai, etc.) |
| `gen_ai.provider.name` | auto-detected | Provider name |
| `gen_ai.operation.name` | `"chat"` | Operation type |
| `gen_ai.request.model` | model name | Requested model |
| `gen_ai.request.temperature` | float | Temperature setting |
| `gen_ai.request.top_p` | float | Top-p sampling |
| `gen_ai.request.max_tokens` | int | Max tokens limit |
| `llm.is_streaming` | true/false | Streaming mode |
| `llm.request.purpose` | classification | Request type (User Chat, Title Gen, etc.) |
| `gen_ai.prompt.0.role` | `"user"` | Last user message role |
| `gen_ai.prompt.0.content` | string | Last user message content |
| `server.address` / `server.port` | host/port | Ollama server coordinates |
| `enduser.id` | SHA-256 hash | Hashed user identifier (privacy-safe) |
| `conversation.fingerprint` | SHA-256 hash | Hashed conversation ID |

**Response attributes** (set by `_set_genai_response()`):

| Attribute | Value | Description |
|-----------|-------|-------------|
| `gen_ai.response.model` | model name | Model that responded |
| `gen_ai.response.finish_reasons` | JSON array | Finish reason(s) |
| `gen_ai.usage.input_tokens` | int | Prompt token count |
| `gen_ai.usage.output_tokens` | int | Completion token count |
| `gen_ai.completion.0.role` | `"assistant"` | Response role |
| `gen_ai.completion.0.content` | string | Response text |

**Span events**: `gen_ai.user.message` (input) and `gen_ai.assistant.message` (output) events carry truncated content for trace search.

---

## Step 5 — Metrics (OTel GenAI Semantic Conventions + Operational)

**What**: 10 custom metrics exported via OTLP/HTTP to Dynatrace, defined in `GenAIMetrics` dataclass.

**Critical configuration**: Dynatrace requires **DELTA temporality** for Counters and Histograms, **CUMULATIVE** for UpDownCounters.

**Metrics list**:

| Metric | Type | Unit | Source |
|--------|------|------|--------|
| `gen_ai.client.operation.duration` | Histogram | `s` | OTel semconv |
| `gen_ai.client.token.usage` | Histogram | `{token}` | OTel semconv |
| `gen_ai.server.time_to_first_token` | Histogram | `s` | OTel semconv |
| `gen_ai.server.time_per_output_token` | Histogram | `s` | OTel semconv |
| `llm.request.count` | Counter | `{request}` | Operational |
| `llm.request.errors` | Counter | `{request}` | Operational |
| `llm.request.active` | UpDownCounter | `{request}` | Operational |
| `llm.stream.chunks` | Counter | `{chunk}` | Operational |
| `llm.token.throughput` | Histogram | `{token}/s` | Operational |
| `llm.request.message_count` | Histogram | `{message}` | Operational |

**Dimensions on each metric**:
- GenAI semconv metrics: `gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`, `server.address`, `server.port`
- `gen_ai.client.token.usage` adds: `gen_ai.token.type` (input/output)
- Operational metrics: `model`, `stream`, `error.type` as applicable

**Histogram bucket boundaries** (configured via `MeterProvider` Views):
- Duration: `[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]`
- Tokens: `[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]`
- TTFT: `[0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, ...]`
- TPOT: `[0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0]`

---

## Step 6 — Logs with Trace Correlation

**What**: Structured application logs exported via OTLP to Dynatrace, automatically correlated with traces via `trace_id` and `span_id`.

**How**: `otel_setup.py` creates a `LoggerProvider` with `BatchLogRecordProcessor` → `OTLPLogExporter`, then attaches a `LoggingHandler` to the root Python logger.

**Log fields** (via `extra={}` dicts):
- `model` — model name
- `duration_s` — request duration in seconds
- `prompt_tokens` / `completion_tokens` — token counts
- `stream` — streaming boolean
- `error.type` — error class name (on failures)

**Result**: Every log line carries `trace_id`, `span_id`, and structured context. In Dynatrace, logs are clickable to jump to the associated trace.

---

## Step 7 — OTel Collector Sidecar (Open WebUI → Dynatrace)

**What**: An OpenTelemetry Collector (`otel/opentelemetry-collector-contrib`) that bridges Open WebUI's native OTel gRPC export to Dynatrace's OTLP/HTTP endpoint.

**Why**: Open WebUI supports OTel traces/metrics natively but only via gRPC. Dynatrace requires OTLP/HTTP with `Api-Token` auth. The collector translates the protocol and adds authentication.

**Config**: `otel-collector/config.yaml`
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  cumulativetodelta: {}

exporters:
  otlphttp:
    endpoint: ${env:DT_OTLP_ENDPOINT}
    headers:
      Authorization: "Api-Token ${env:DT_OTLP_TOKEN}"

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlphttp]
    metrics:
      receivers: [otlp]
      processors: [cumulativetodelta]
      exporters: [otlphttp]
```

**Key**: The `cumulativetodelta` processor converts Open WebUI's CUMULATIVE counters to DELTA temporality (required by Dynatrace).

**Open WebUI env vars** (in `docker-compose.yml`):
```
ENABLE_OTEL=true
ENABLE_OTEL_TRACES=true
ENABLE_OTEL_METRICS=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_EXPORTER_OTLP_INSECURE=true
OTEL_SERVICE_NAME=open-webui
```

---

## Step 8 — Dynatrace RUM (Real User Monitoring)

**What**: Injected Dynatrace JavaScript agent into Open WebUI for frontend monitoring.

**How**: The `open-webui/entrypoint.sh` script injects the RUM JS tag into Open WebUI's `index.html` at container startup:

```html
<script type="text/javascript"
  src="https://js-cdn.dynatrace.com/jstag/18b1df4492a/bf12470wrz/1415e268575ba0e2_complete.js"
  crossorigin="anonymous"></script>
```

**What it captures**: Page loads, XHR/fetch calls, Web Vitals (LCP, FID, CLS), JavaScript errors, user actions, and session replay data.

---

## Step 9 — User Tagging (dtrum.identifyUser)

**What**: Injected a script that reads the logged-in user's name from Open WebUI's JWT token and calls `dtrum.identifyUser()` to tag RUM sessions.

**How**: The entrypoint script injects a `<script>` before `</body>` that:
1. Reads the JWT from `localStorage("token")`
2. Base64-decodes the payload and extracts the `name` field
3. Calls `dtrum.identifyUser(name)`
4. Hooks `Storage.prototype.setItem` to re-tag on login/logout

**Result**: Every RUM session in Dynatrace shows the user's name, enabling per-user performance analysis.

---

## Step 10 — Serif Typography (CSS Injection)

**What**: Custom serif font theme (Playfair Display + Lora) injected into Open WebUI.

**How**: `open-webui/custom.css` is mounted into the container, and `entrypoint.sh` injects it as a `<style>` tag into `index.html`.

---

## Step 11 — Dynatrace Dashboard (Leadership-Grade)

**What**: A 44-tile dashboard across 6 sections, deployed via `dtctl apply -f dashboard.json`.

**Files**: `dashboard.json` (source of truth), `dashboard.yaml` (synced copy)

### Section 1 — Executive Summary
| Tile | Visualization | Data |
|------|--------------|------|
| Total Conversations | Single Value | `countDistinct(conversation.fingerprint)` from spans |
| Active Users | Single Value | `countDistinct(enduser.id)` from spans |
| Availability SLI | Single Value | `(total - errors) / total * 100` from spans (color thresholds) |
| Total Requests | Single Value | `llm.request.count` metric |
| Avg Response Time | Single Value | `gen_ai.client.operation.duration` metric (color thresholds) |
| Error Rate % | Single Value | Computed from `llm.request.errors` / `llm.request.count` (color thresholds) |
| Request Volume Trend | Line Chart | `llm.request.count` timeseries |
| Request Purpose Breakdown | Pie Chart | Span count by `llm.request.purpose` |

### Section 2 — Cost & Token Economics
| Tile | Visualization | Data |
|------|--------------|------|
| Total Tokens | Single Value | `gen_ai.client.token.usage` metric |
| Avg Tokens / Conversation | Single Value | Total tokens / distinct conversations from spans |
| Output / Input Ratio | Single Value | Output tokens / input tokens from spans |
| Token Consumption Trend (I/O) | Stacked Area | `gen_ai.client.token.usage` by `gen_ai.token.type` |
| Token Spend by Model | Bar Chart | Tokens summed by `gen_ai.request.model` from spans |
| Token Spend by Purpose | Bar Chart | Tokens summed by `llm.request.purpose` from spans |
| Avg Tokens per Request Trend | Line Chart | Token usage / request count timeseries |

### Section 3 — Model Performance Scorecard
| Tile | Visualization | Data |
|------|--------------|------|
| Model Comparison | Table | Requests, avg/p50/p95 duration, avg tokens per model from spans |
| Throughput by Model (tok/s) | Bar Chart | `llm.token.throughput` by model |
| TTFT by Model | Bar Chart | `gen_ai.server.time_to_first_token` by model |
| Response Time by Model | Line Chart | `gen_ai.client.operation.duration` by model |
| p95 Latency Trend by Model | Line Chart | `percentile(duration, 95)` makeTimeseries from spans |
| Stream vs Non-Stream by Model | Bar Chart | Span count by model × streaming mode |

### Section 4 — User Experience & Quality
| Tile | Visualization | Data |
|------|--------------|------|
| TTFT SLI (% < 2s) | Single Value | Fast requests / total (color thresholds) |
| p95 TTFT | Single Value | 95th percentile span duration |
| p95 Response Time | Single Value | 95th percentile span duration (color thresholds) |
| TTFT Trend | Line Chart | `gen_ai.server.time_to_first_token` timeseries |
| TPOT Trend | Line Chart | `gen_ai.server.time_per_output_token` timeseries |
| Throughput Trend | Line Chart | `llm.token.throughput` timeseries |
| Top 10 Slowest Prompts | Table | Spans sorted by duration, showing prompt preview + tokens |

### Section 5 — Open WebUI Operations
| Tile | Visualization | Data |
|------|--------------|------|
| WebUI Requests | Single Value | Server spans from `open-webui` service |
| WebUI Avg Latency | Single Value | Avg duration of server spans |
| WebUI Error Rate % | Single Value | 5xx responses / total (color thresholds) |
| DB Query Count | Single Value | Spans with `db.system` set |
| WebUI Latency Trend | Line Chart | Avg duration makeTimeseries |
| DB Query Latency Trend | Line Chart | DB span avg duration makeTimeseries |
| Top Endpoints by Volume | Table | Endpoints with count, avg duration, error count |
| Slowest DB Queries | Table | DB spans sorted by duration, showing statement |

### Section 6 — Traces & Logs
| Tile | Visualization | Data |
|------|--------------|------|
| Recent GenAI Traces | Table | Latest 20 spans with model, purpose, tokens, duration |
| Recent Backend Logs | Table | Latest 20 logs with content, level, model, tokens, trace_id |

---

## Environment Variables Summary

| Variable | Purpose | Where Set |
|----------|---------|-----------|
| `OLLAMA_BASE_URL` | Ollama API endpoint | `.env` |
| `DEFAULT_MODEL` | Fallback model name | `.env` |
| `TRACELOOP_BASE_URL` | Dynatrace OTLP endpoint for traces/metrics/logs | `.env` |
| `TRACELOOP_HEADERS` | Auth header for Traceloop trace export | `docker-compose.yml` |
| `DT_OTLP_TOKEN` | Dynatrace API token (metrics, logs, collector) | `.env` |
| `DT_OTLP_ENDPOINT` | Dynatrace OTLP endpoint (for OTel Collector) | `docker-compose.yml` |
| `ENABLE_OLLAMA_API` | Disable direct Ollama in Open WebUI (`false`) | `docker-compose.yml` |
| `OPENAI_API_BASE_URL` | Routes Open WebUI → FastAPI backend | `docker-compose.yml` |
| `ENABLE_OTEL` | Enable Open WebUI native OTel export | `docker-compose.yml` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector gRPC endpoint for Open WebUI | `docker-compose.yml` |
| `OTEL_SERVICE_NAME` | Service name for Open WebUI spans | `docker-compose.yml` |

---

## Key Decisions & Gotchas

1. **DELTA temporality** — Dynatrace rejects CUMULATIVE for Counters/Histograms. Always use DELTA. UpDownCounters must stay CUMULATIVE.
2. **MeterProvider before Traceloop.init()** — Must set the global MeterProvider before Traceloop initializes, otherwise Traceloop creates its own.
3. **OTel Collector for Open WebUI** — Open WebUI only supports gRPC export. Dynatrace requires OTLP/HTTP. The collector bridges this gap and applies `cumulativetodelta` conversion.
4. **`llm.request.purpose` is span-only** — Request purpose (User Chat, Title Gen, etc.) is a span attribute, NOT a metric dimension. Dashboard tiles needing purpose breakdown must use `fetch spans`, not `timeseries`.
5. **Ollama native timing** — Non-streaming responses include `prompt_eval_duration` and `eval_duration` in nanoseconds, used directly for precise TTFT and TPOT calculations.
6. **Privacy-safe user tracking** — `enduser.id` and `conversation.fingerprint` are SHA-256 hashed before being set as span attributes.
7. **RUM injection is idempotent** — The entrypoint checks for existing tags before injecting, safe across container restarts.
8. **OTel Collector config mount path** — Must be `/etc/otelcol-contrib/config.yaml` (not `/etc/otel/config.yaml`). The contrib image uses a different default path.
9. **Dashboard grid** — Dynatrace dashboards use a 20-column grid. Tile `x + w` must never exceed 20.
