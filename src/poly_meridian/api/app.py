"""FastAPI app for the operator dashboard. Mounted on port 8000 by main.py."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from poly_meridian.api.state import AgentStateBroker
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.api.app")


def build_app(broker: AgentStateBroker) -> FastAPI:
    app = FastAPI(
        title="Poly Meridian Operator API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # CORS for local dev — UI runs on :3000, API on :8000.
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
                # Send initial snapshot.
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

    @app.exception_handler(404)
    async def not_found(_request: Any, _exc: Any) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found"})

    return app


def _sse_event(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, default=str)
    return f"data: {data}\n\n".encode("utf-8")


async def _heartbeat_loop(broker: AgentStateBroker) -> None:
    while True:
        await asyncio.sleep(15)
        broker.heartbeat()
