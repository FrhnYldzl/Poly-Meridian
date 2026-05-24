# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# uv for fast, deterministic installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY src ./src

RUN uv pip install --system -e ".[polymarket]"

COPY config ./config
COPY scripts ./scripts

ENV MODE=paper \
    PORT=8000

# Railway routes traffic to whatever port the platform assigns via $PORT.
# Our settings.py reads $PORT with fallback to $PROMETHEUS_PORT → 8000.
EXPOSE 8000

# Healthcheck so Railway / Docker can confirm the agent is alive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

CMD ["python", "-m", "poly_meridian.main"]
