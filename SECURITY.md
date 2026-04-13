# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Do not open a public issue.** Instead, email the maintainer directly or use [GitHub's private vulnerability reporting](https://github.com/theharithsa/Local-LLM-Application-with-OpenLLMetry/security/advisories/new).

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You can expect an initial response within 72 hours.

## Security Considerations

- **Secrets**: All credentials (Dynatrace tokens, API keys) are stored in `.env` and never committed to the repository.
- **Network**: The FastAPI backend is the only service that communicates with Ollama. Open WebUI does not have direct access.
- **RUM injection**: The Dynatrace RUM script is injected via an idempotent entrypoint — it uses a pinned CDN URL and does not execute arbitrary code.
- **Local-only by default**: This application is designed to run locally. If you expose it to a network, add proper authentication and TLS.
