FROM python:3.11-slim

WORKDIR /app

# Install llm-otel-kit (from PyPI or local)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE {{ cookiecutter.backend_port }}

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{{ cookiecutter.backend_port }}"]
