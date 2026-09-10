# Single-stage, single-service image. The API and the console are one process,
# which is what lets this run on a free tier that gives you exactly one.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY web/ ./web/
COPY scripts/ ./scripts/

# Hugging Face Spaces runs containers as a non-root user and expects port 7860;
# Render sets $PORT. Defaulting to 7860 keeps both working.
ENV PORT=7860
EXPOSE 7860

RUN useradd --create-home --uid 1000 yogya && chown -R yogya:yogya /app
USER yogya

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"

CMD ["sh", "-c", "uvicorn yogya.api.app:app --host 0.0.0.0 --port ${PORT}"]
