FROM python:3.11-slim

WORKDIR /app

# Copy the llm-otel-kit package first (dependency of requirements.txt)
COPY llm-otel-kit /app/llm-otel-kit

# Install dependencies (better layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
