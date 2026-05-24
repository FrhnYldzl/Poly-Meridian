# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build the Next.js dashboard to static HTML ----------
FROM node:22-alpine AS web-builder

WORKDIR /web
COPY web/package.json web/package-lock.json* web/.npmrc* ./
RUN npm install --legacy-peer-deps

COPY web/ ./
RUN npm run build
# `next build` with `output: "export"` writes static HTML/JS to /web/out

# ---------- Stage 2: the Python agent + bundled static UI ----------
FROM python:3.12-slim AS app

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

# pyproject + README needed for hatchling to validate metadata.
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system -e ".[polymarket]"

COPY config ./config
COPY scripts ./scripts

# Bring in the built dashboard.
COPY --from=web-builder /web/out /app/static

ENV MODE=paper \
    PORT=8000 \
    STATIC_DIR=/app/static

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

CMD ["python", "-m", "poly_meridian.main"]
