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
       │ Dynatrace RUM JS      │ OTLP/HTTP
       ▼                       ▼
┌──────────────────────────────────────────┐
│           Dynatrace Platform             │
│  (Traces, Metrics, Logs, RUM, Dashboard) │
└──────────────────────────────────────────┘
```

**Key design decision**: Open WebUI does NOT connect to Ollama directly. All chat requests route through the FastAPI backend (`ENABLE_OLLAMA_API=false`), which acts as an OpenAI-compatible proxy. This ensures every LLM call is instrumented.

---

## Step 1 — FastAPI Backend as OpenAI-Compatible Proxy

**What**: A Python FastAPI app that translates OpenAI API format to Ollama's API format.

**Why**: Open WebUI speaks OpenAI API. Ollama has its own format. The proxy bridges them and gives us a single instrumentation point.

**Key endpoints**:
- `POST /v1/chat/completions` → translates to Ollama `POST /api/chat`
- `GET /v1/models` → translates to Ollama `GET /api/tags`

**Files**: `backend/main.py`, `backend/Dockerfile`, `backend/requirements.txt`

---

## Step 2 — Distributed Traces (OpenLLMetry / Traceloop SDK)

**What**: Integrated [OpenLLMetry](https://github.com/traceloop/openllmetry) (Traceloop SDK) to automatically instrument the backend and generate OpenTelemetry traces.

**How**:
1. Added `traceloop-sdk>=0.59.0` to `requirements.txt`
2. Initialized in `main.py`:
   ```python
   from traceloop.sdk import Traceloop
   Traceloop.init(app_name="local-llm-backend", disable_batch=False)
   ```
3. Decorated handler functions with `@workflow`:
   - `chat_completions` — root span for every chat request
   - `_stream_ollama` — child span for streaming responses
   - `_non_stream_ollama` — child span for non-streaming responses
4. Added manual HTTP client child spans using `tracer.start_as_current_span("POST")` around the `httpx` call to Ollama, giving 3 spans per trace.

**Environment variables** (in `docker-compose.yml`):
- `TRACELOOP_BASE_URL` — Dynatrace OTLP endpoint
- `TRACELOOP_HEADERS` — `Authorization=Api-Token%20<token>`

**Result**: Every chat request produces a distributed trace with 3 spans: `chat_completions.workflow` → `stream_ollama.workflow` or `non_stream_ollama.workflow` → `POST /api/chat`.

---

## Step 3 — GenAI Span Attributes (OTel Semantic Conventions)

**What**: Added rich span attributes following the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) so traces contain the full context of each LLM call.

**Attributes set on spans**:

| Attribute | Value | Description |
|-----------|-------|-------------|
| `gen_ai.operation.name` | `"chat"` | Operation type |
| `gen_ai.system` | `"ollama"` | LLM system |
| `gen_ai.provider.name` | `"ollama"` | Provider |
| `gen_ai.request.model` | model name | Requested model |
| `gen_ai.request.stream` | true/false | Streaming mode |
| `gen_ai.request.temperature` | float | Temperature setting |
| `gen_ai.prompt` | JSON string | Full prompt (all messages) |
| `gen_ai.input.messages` | JSON string | Input messages (GenAI semconv format) |
| `gen_ai.completion` | JSON string | LLM response text |
| `gen_ai.output.messages` | JSON string | Output messages (GenAI semconv format) |
| `gen_ai.response.id` | UUID | Response identifier |
| `gen_ai.response.finish_reasons` | JSON array | Finish reason(s) |
| `gen_ai.usage.input_tokens` | int | Prompt token count |
| `gen_ai.usage.output_tokens` | int | Completion token count |
| `server.address` / `server.port` | host/port | Ollama server coordinates |

---

## Step 4 — Metrics (OTel GenAI Semantic Conventions + Operational)

**What**: 11 custom metrics exported via OTLP/HTTP to Dynatrace.

**Critical configuration**: Dynatrace requires **DELTA temporality** for Counters and Histograms. Without this, metrics return 400 errors.

```python
from opentelemetry.sdk.metrics.export import AggregationTemporality

exporter = OTLPMetricExporter(
    preferred_temporality={
        Counter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.CUMULATIVE,
    }
)
```

**Metrics list**:

| Metric | Type | Unit | Source |
|--------|------|------|--------|
| `gen_ai.client.operation.duration` | Histogram | seconds | OTel semconv |
| `gen_ai.client.token.usage` | Histogram | tokens | OTel semconv |
| `gen_ai.server.time_to_first_token` | Histogram | seconds | OTel semconv |
| `gen_ai.server.time_per_output_token` | Histogram | seconds | OTel semconv |
| `llm.request.count` | Counter | — | Operational |
| `llm.request.errors` | Counter | — | Operational |
| `llm.request.active` | UpDownCounter | — | Operational |
| `llm.stream.chunks` | Counter | — | Operational |
| `llm.token.throughput` | Histogram | tokens/s | Operational |
| `llm.request.message_count` | Histogram | — | Operational |

**Dimensions on each metric**:
- GenAI semconv metrics carry: `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `server.address`, `server.port`
- `gen_ai.client.token.usage` adds: `gen_ai.token.type` (input/output)
- Operational metrics carry: `model`, `stream`, `error.type` as applicable

**Histogram bucket boundaries** configured via `MeterProvider` Views matching the OTel GenAI spec:
- Duration: `[0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92]`
- Tokens: `[1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576]`
- TTFT: `[0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.12, 0.14, ...]`
- TPOT: `[0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0]`

---

## Step 5 — Logs with Trace Correlation

**What**: Application logs exported via OTLP to Dynatrace, automatically correlated with traces via `trace_id` and `span_id`.

**How**:
```python
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

log_provider = LoggerProvider()
log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
logging.getLogger().addHandler(handler)
```

**Result**: Every log line (`"Chat completion request"`, `"Chat completion finished"`, etc.) carries `trace_id`, `span_id`, `model`, `stream`, and other context fields. In Dynatrace, logs are clickable to jump to the associated trace.

---

## Step 6 — Dynatrace RUM (Real User Monitoring)

**What**: Injected Dynatrace JavaScript agent into Open WebUI for frontend monitoring.

**How**: The `open-webui/entrypoint.sh` script injects the RUM JS tag into Open WebUI's `index.html` at container startup:

```html
<script type="text/javascript"
  src="https://js-cdn.dynatrace.com/jstag/18b1df4492a/bf12470wrz/1415e268575ba0e2_complete.js"
  crossorigin="anonymous"></script>
```

**What it captures**: Page loads, XHR/fetch calls, Web Vitals (LCP, FID, CLS), JavaScript errors, user actions, and session replay data.

---

## Step 7 — User Tagging (dtrum.identifyUser)

**What**: Injected a script that reads the logged-in user's name from Open WebUI's JWT token and calls `dtrum.identifyUser()` to tag RUM sessions.

**How**: The entrypoint script injects a `<script>` before `</body>` that:
1. Reads the JWT from `localStorage("token")`
2. Base64-decodes the payload and extracts the `name` field
3. Calls `dtrum.identifyUser(name)`
4. Hooks `Storage.prototype.setItem` to re-tag on login/logout

**Result**: Every RUM session in Dynatrace shows the user's name, enabling per-user performance analysis.

---

## Step 8 — Serif Typography (CSS Injection)

**What**: Custom serif font theme (Playfair Display + Lora) injected into Open WebUI.

**How**: `open-webui/custom.css` is mounted into the container, and `entrypoint.sh` injects it as a `<style>` tag into `index.html`.

---

## Step 9 — Dynatrace Dashboard

**What**: A comprehensive dashboard deployed via `dtctl` with 2 sections and 21 tiles.

**File**: `dashboard.yaml` (deployable with `dtctl apply -f dashboard.yaml`)

### Business Section
| Tile | Visualization | Data |
|------|--------------|------|
| Total Requests | Single Value | `llm.request.count` |
| Total Tokens | Single Value | `gen_ai.client.token.usage` |
| Avg Response Time | Single Value | `gen_ai.client.operation.duration` |
| Avg TTFT | Single Value | `gen_ai.server.time_to_first_token` |
| Request Volume | Bar Chart | `llm.request.count` by model |
| Token Usage (I/O) | Stacked Area | `gen_ai.client.token.usage` by type |
| Response Time Trend | Line Chart | `gen_ai.client.operation.duration` by model |
| Token Consumption | Pie Chart | `gen_ai.client.token.usage` by type |
| Top 10 Slowest Prompts | Table | Spans joined via `lookup` (prompt + tokens + duration) |

### Technical Section
| Tile | Visualization | Data |
|------|--------------|------|
| Avg TPOT | Single Value | `gen_ai.server.time_per_output_token` |
| Avg Throughput | Single Value | `llm.token.throughput` |
| Total Errors | Single Value | `llm.request.errors` (color thresholds) |
| Unique Traces | Single Value | `countDistinct(trace.id)` from spans |
| TTFT Over Time | Line Chart | `gen_ai.server.time_to_first_token` by model |
| TPOT Over Time | Line Chart | `gen_ai.server.time_per_output_token` by model |
| Throughput Over Time | Line Chart | `llm.token.throughput` by model |
| Error Rate | Bar Chart | `llm.request.errors` by model |
| Recent Traces | Table | Spans with model, tokens, duration |
| Recent Logs | Table | Logs with trace_id correlation |

---

## Environment Variables Summary

| Variable | Purpose | Where Set |
|----------|---------|-----------|
| `OLLAMA_BASE_URL` | Ollama API endpoint | `.env` |
| `DEFAULT_MODEL` | Fallback model name | `.env` |
| `TRACELOOP_BASE_URL` | Dynatrace OTLP endpoint for traces | `.env` |
| `TRACELOOP_HEADERS` | Auth header for trace export | `docker-compose.yml` |
| `DT_OTLP_TOKEN` | Dynatrace API token (metrics + logs) | `.env` |
| `ENABLE_OLLAMA_API` | Disable direct Ollama in Open WebUI | `docker-compose.yml` |
| `OPENAI_API_BASE_URL` | Routes Open WebUI → FastAPI backend | `docker-compose.yml` |

---

## Key Decisions & Gotchas

1. **DELTA temporality** — Dynatrace rejects CUMULATIVE for Counters/Histograms. Always use DELTA.
2. **MeterProvider before Traceloop.init()** — Must set the global MeterProvider before Traceloop initializes, otherwise Traceloop creates its own.
3. **Streaming token counts** — For streaming requests, `chat_completions.workflow` closes immediately (returns `StreamingResponse`). Token counts are on the child `stream_ollama.workflow` span. The dashboard uses `lookup` to join parent prompt with child tokens.
4. **Ollama native timing** — Non-streaming responses include `prompt_eval_duration` and `eval_duration` in nanoseconds, used directly for precise TTFT and TPOT calculations.
5. **RUM injection is idempotent** — The entrypoint checks for existing tags before injecting, safe across container restarts.
6. **User tagging from JWT** — Open WebUI stores the JWT in `localStorage("token")`. The payload contains the user's name.
