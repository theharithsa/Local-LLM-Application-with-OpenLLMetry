# Contributing to Local LLM App

Thanks for your interest in contributing! Here's how to get started.

## Getting Started

1. **Fork** the repository and clone your fork.
2. Make sure [Docker Desktop](https://www.docker.com/products/docker-desktop/) and [Ollama](https://ollama.com/download) are installed.
3. Copy `.env.example` to `.env` and fill in any required values.
4. Run `docker compose up -d --build` to start the stack locally.

## Development Workflow

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes.
3. Test locally with `docker compose up -d --build`.
4. Commit using clear, descriptive messages:
   ```
   feat: add /v1/embeddings endpoint with full instrumentation
   fix: correct DELTA temporality for token usage histogram
   ```
5. Push your branch and open a Pull Request.

## Code Guidelines

- **Observability-first**: Every new endpoint or feature must include traces, metrics, and structured logging from the start. See [OBSERVABILITY.md](OBSERVABILITY.md).
- **Python**: Use `httpx.AsyncClient` for HTTP calls, Pydantic for schemas, `os.getenv()` for config.
- **Logging**: Use `logger` with structured `extra={}` dicts — never `print()`.
- **Docker**: Keep entrypoint scripts idempotent. Use layer-friendly `COPY` ordering in Dockerfiles.
- **No secrets**: Never commit `.env` files or hardcoded credentials.

## Reporting Bugs

Open an [issue](https://github.com/theharithsa/Local-LLM-Application-with-OpenLLMetry/issues) with:
- Steps to reproduce
- Expected vs actual behavior
- Docker and Ollama versions

## Pull Request Checklist

- [ ] Code follows the project conventions above
- [ ] New endpoints include `@workflow` decorator and span attributes
- [ ] Metrics use correct temporality (DELTA for counters/histograms, CUMULATIVE for UpDownCounters)
- [ ] Tested locally with `docker compose up -d --build`
- [ ] No secrets or `.env` files included
