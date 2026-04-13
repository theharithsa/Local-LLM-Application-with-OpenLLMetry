# Local LLM App

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A self-hosted AI chatbot powered by [Open WebUI](https://github.com/open-webui/open-webui) and [Ollama](https://ollama.com), with **full-stack observability** (distributed traces, metrics, logs, and Real User Monitoring) exported to [Dynatrace](https://www.dynatrace.com).

All LLM traffic is routed through a FastAPI backend that acts as an OpenAI-compatible proxy, ensuring every request is instrumented end-to-end.

```
Open WebUI  →  FastAPI Backend  →  Ollama (on host)
     │                │
     │ RUM JS         │ OTLP/HTTP
     ▼                ▼
        Dynatrace Platform
```

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Adding Models](#adding-models)
- [Project Structure](#project-structure)
- [Observability](#observability)
- [Contributing](#contributing)
- [License](#license)

---

## Prerequisites

Before you begin, make sure the following are installed and running on your machine:

1. **Docker Desktop** — [download here](https://www.docker.com/products/docker-desktop/)
2. **Ollama** — [download here](https://ollama.com/download)
3. At least **one model** pulled in Ollama:
   ```bash
   ollama pull gemma4:26b
   ```

> **Note:** Ollama must be running on the host machine (`ollama serve` or as a system service). The Docker containers connect to it via `host.docker.internal`.

---

## Quick Start

1. **Clone the repository:**
   ```bash
   git clone https://github.com/theharithsa/Local-LLM-Application-with-OpenLLMetry.git
   cd Local-LLM-Application-with-OpenLLMetry
   ```

2. **Create your environment file:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in your Dynatrace credentials (optional — the app works without them, just no observability export).

3. **Start the app:**
   ```bash
   docker compose up -d
   ```

4. **Open the UI:**
   Navigate to **http://localhost:3000** in your browser. Create an account on first launch — it's stored locally.

### Stop

```bash
docker compose down
```

### Rebuild (after code changes)

```bash
docker compose up -d --build
```

---

## Configuration

All configuration is done via the `.env` file. See [`.env.example`](.env.example) for all available variables.

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_BASE_URL` | Ollama API endpoint on the host | `http://host.docker.internal:11434` |
| `DEFAULT_MODEL` | Default model for chat | `gemma4:26b` |
| `TRACELOOP_BASE_URL` | Dynatrace OTLP endpoint (optional) | — |
| `DT_OTLP_TOKEN` | Dynatrace API token (optional) | — |
| `WEBUI_SECRET_KEY` | Open WebUI session secret | `change-me-in-production` |

> **Tip:** The app runs fully offline without Dynatrace credentials. Set them when you want observability data exported.

---

## Adding Models

Pull any model in Ollama — it automatically appears in the UI:

```bash
ollama pull llama3
ollama pull mistral
ollama pull phi3
```

---

## Project Structure

```
├── backend/
│   ├── main.py               # FastAPI proxy (Ollama ↔ OpenAI format) + OTel instrumentation
│   ├── requirements.txt      # Python dependencies
│   └── Dockerfile
├── open-webui/
│   ├── custom.css            # Serif typography theme
│   └── entrypoint.sh         # Injects CSS, Dynatrace RUM tag, and user tagging
├── docker-compose.yml        # Two services: backend + open-webui
├── dashboard.yaml            # Dynatrace dashboard (deployable via dtctl)
├── OBSERVABILITY.md          # Detailed observability implementation guide
└── .env                      # Environment variables (not committed)
```

---

## Observability

This project is built **observability-first**. Every LLM request produces:

- **Distributed traces** via [OpenLLMetry](https://github.com/traceloop/openllmetry) (Traceloop SDK) with GenAI semantic convention attributes
- **11 custom metrics** following OTel GenAI semantic conventions (operation duration, token usage, TTFT, TPOT, throughput)
- **Structured logs** exported via OTLP with trace correlation
- **Real User Monitoring** via Dynatrace RUM JS agent with automatic user tagging

See [OBSERVABILITY.md](OBSERVABILITY.md) for the full implementation walkthrough.

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a pull request.

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

