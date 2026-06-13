# Stage 1: Builder
FROM python:3.11-slim as builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

COPY . .

RUN groupadd -r botuser && useradd -r -g botuser -u 1000 botuser && \
    mkdir -p data && \
    chown -R botuser:botuser /app

USER botuser

EXPOSE 5000

ENV TELEGRAM_TOKEN="" \
    DATABASE_URL="" \
    REDIS_URL="" \
    SESSION_SECRET="" \
    OWNER_ID="" \
    WIFU_ID="" \
    MODE="polling" \
    WEBHOOK_URL="" \
    RENDER_URL="" \
    PORT="5000" \
    LOG_LEVEL="INFO"

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000').read()" || exit 1

CMD ["python", "main.py"]
