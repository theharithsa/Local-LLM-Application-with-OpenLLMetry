# Local LLM App — Development Rules

## Architecture (non-negotiable)

- Open WebUI NEVER connects to Ollama directly. All LLM traffic routes through the FastAPI backend (`ENABLE_OLLAMA_API=false`). This ensures every call is instrumented.
- The backend is the sole instrumentation point. It acts as an OpenAI-compatible proxy translating to Ollama's API format.
- Three-tier flow: `Open WebUI → FastAPI Backend → Ollama`. No shortcuts.

## Observability-First Development

Every new feature, endpoint, or integration MUST include observability from the start — not as a follow-up.

- **Traces**: Decorate new handler functions with `@workflow(name="...")` from `traceloop.sdk.decorators`. Add manual child spans with `_tracer.start_as_current_span()` for outbound HTTP calls.
- **Metrics**: Add relevant counters/histograms using the existing `_meter`. Follow OTel GenAI semantic conventions for LLM-related metrics. Use descriptive `name`, `description`, and `unit` fields.
- **Logs**: Use the module `logger` (not `print()`). Include structured `extra={}` dicts with contextual fields (model, duration, token counts). Logs are exported to Dynatrace via OTLP.
- **Span attributes**: Set GenAI semantic convention attributes (`gen_ai.*`, `server.*`) on spans. Format input/output messages using `_format_input_messages()` / `_format_output_messages()`.
- **RUM**: Frontend changes to Open WebUI must preserve the Dynatrace RUM tag and `dtrum.identifyUser()` injection in `entrypoint.sh`.

## Python / FastAPI Conventions

- Use `httpx.AsyncClient` for all outbound HTTP calls (async, not `requests`).
- Define request/response schemas with Pydantic `BaseModel`.
- Configuration via `os.getenv()` with sensible defaults. No hardcoded URLs or credentials.
- Traceloop SDK init MUST come AFTER `MeterProvider` setup to avoid conflicts.
- Type hints on function signatures. Use `list[dict]` style (Python 3.11+), not `typing.List`.

## OTel / Dynatrace Export Rules

- Dynatrace requires **DELTA temporality** for Counters and Histograms, **CUMULATIVE** for UpDownCounters. Getting this wrong returns 400 errors.
- Use `ExplicitBucketHistogramAggregation` with GenAI semconv bucket boundaries for duration, token, TTFT, and TPOT histograms.
- OTLP exports use `Api-Token` auth header format: `Authorization: Api-Token <token>`.
- Metrics export interval: 30 seconds (`export_interval_millis=30_000`).

## Docker / Infrastructure

- `docker-compose.yml` uses `host.docker.internal:host-gateway` for container-to-host communication (Ollama on host).
- Dockerfile: copy `requirements.txt` and `pip install` before copying app code (layer caching).
- Entrypoint scripts must be idempotent — check before injecting (e.g., `grep -q` guards).
- Secrets via `.env` file and `${VAR}` interpolation in Compose. Never commit `.env`.
