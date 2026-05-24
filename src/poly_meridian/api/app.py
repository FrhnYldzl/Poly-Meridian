"""FastAPI app for the operator dashboard. Mounted on port 8000 by main.py.

Two surfaces in one process:
  - REST + SSE under /api/* + /health  (agent state)
  - Static Next.js dashboard at /       (same-origin, no CORS dance)

The Docker build copies `web/out/` into `/app/static` so the dashboard
ships in the same image as the agent — single Railway service, one URL.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from poly_meridian.api.state import AgentStateBroker
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.api.app")

STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))


def build_app(broker: AgentStateBroker) -> FastAPI:
    app = FastAPI(
        title="Poly Meridian Operator API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # CORS still useful for local dev where the UI may be on :3001.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return broker.snapshot.asdict()

    @app.get("/api/settings")
    async def settings_info() -> dict[str, Any]:
        s = get_settings()
        return {
            "mode": str(s.mode),
            "clob_host": s.polymarket_clob_host,
            "gamma_host": s.polymarket_gamma_host,
            "log_level": s.log_level,
            "sentiment_window_sec": s.sentiment_window_sec,
        }

    @app.post("/api/kill-switch/engage")
    async def engage_kill_switch(reason: str = "manual via UI") -> dict[str, Any]:
        broker.update_kill_switch(engaged=True, reason=reason)
        return {"engaged": True, "reason": reason}

    @app.post("/api/kill-switch/disengage")
    async def disengage_kill_switch() -> dict[str, Any]:
        broker.update_kill_switch(engaged=False, reason=None)
        return {"engaged": False}

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def event_gen() -> AsyncIterator[bytes]:
            q = await broker.subscribe()
            heartbeat_task = asyncio.create_task(_heartbeat_loop(broker))
            try:
                init = {"type": "snapshot", "data": broker.snapshot.asdict()}
                yield _sse_event(init)
                while True:
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield _sse_event({"type": "heartbeat"})
                        continue
                    yield _sse_event(evt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("sse.client_error", error=str(exc))
            finally:
                heartbeat_task.cancel()
                await broker.unsubscribe(q)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ----- Static dashboard mount -----
    # `next build` with output: "export" writes to /web/out, copied to
    # /app/static by the Dockerfile.
    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        # Serve Next.js's _next assets at /_next so chunk URLs resolve.
        next_assets = STATIC_DIR / "_next"
        if next_assets.exists():
            app.mount("/_next", StaticFiles(directory=next_assets), name="next_assets")

        @app.get("/", include_in_schema=False)
        async def root_index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        # SPA fallback — any unmatched non-API route returns the matching
        # exported .html if it exists, otherwise index.html.
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith(("api/", "health", "_next/")):
                # Let FastAPI's own 404 handle it.
                return JSONResponse(status_code=404, content={"error": "not_found"})  # type: ignore[return-value]
            # Try exact .html (e.g. /markets → /markets.html or /markets/index.html)
            candidate_dir = STATIC_DIR / full_path / "index.html"
            candidate_html = STATIC_DIR / f"{full_path}.html"
            if candidate_dir.exists():
                return FileResponse(candidate_dir)
            if candidate_html.exists():
                return FileResponse(candidate_html)
            # Asset path (e.g. /favicon.ico)
            asset = STATIC_DIR / full_path
            if asset.is_file():
                return FileResponse(asset)
            # Fallback to root index (dashboard SPA handles the route client-side)
            return FileResponse(STATIC_DIR / "index.html")

        log.info("api.static_mounted", path=str(STATIC_DIR))
    else:
        # No static build available — keep the JSON-friendly 404 so anyone
        # hitting / still gets a useful response in API-only deployments.
        @app.exception_handler(404)
        async def not_found(_request: Any, _exc: Any) -> JSONResponse:
            return JSONResponse(status_code=404, content={"error": "not_found"})

        log.info("api.static_missing", path=str(STATIC_DIR))

    return app


def _sse_event(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, default=str)
    return f"data: {data}\n\n".encode("utf-8")


async def _heartbeat_loop(broker: AgentStateBroker) -> None:
    while True:
        await asyncio.sleep(15)
        broker.heartbeat()
