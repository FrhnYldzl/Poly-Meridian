"""FastAPI app for the operator dashboard. Mounted on port 8000 by main.py."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        """Friendly landing — the agent serves JSON APIs, not the UI.
        Tells visitors where to find the dashboard + docs."""
        snap = broker.snapshot
        uptime_h = snap.uptime_sec / 3600
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Poly Meridian — Agent API</title>
<style>
  body{{background:#0a0a0b;color:#e5e5e7;font-family:ui-monospace,Menlo,monospace;
       margin:0;padding:48px;line-height:1.6;font-size:13px}}
  h1{{color:#ff9e0a;font-size:18px;letter-spacing:.2em;text-transform:uppercase;margin:0 0 24px}}
  .stat{{display:inline-block;margin-right:24px}}.lbl{{color:#5c5c63;font-size:11px;text-transform:uppercase;letter-spacing:.1em}}
  .v{{color:#fafafa;font-size:18px;font-weight:600}}.amber{{color:#ff9e0a}}
  ul{{padding-left:18px;color:#22d3ee}}a{{color:#22d3ee;text-decoration:none}}
  hr{{border:0;border-top:1px solid #262629;margin:24px 0}}
  pre{{background:#111114;padding:12px;border:1px solid #262629;border-radius:4px;overflow-x:auto}}
</style></head>
<body>
<h1>POLY • MERIDIAN — Agent API</h1>
<div>
  <div class="stat"><div class="lbl">Mode</div><div class="v amber">{snap.mode}</div></div>
  <div class="stat"><div class="lbl">NAV</div><div class="v">${snap.nav_usd:,.0f}</div></div>
  <div class="stat"><div class="lbl">Markets</div><div class="v">{snap.markets_watched}</div></div>
  <div class="stat"><div class="lbl">Ticks</div><div class="v">{snap.pipeline_ticks_total:,}</div></div>
  <div class="stat"><div class="lbl">Uptime</div><div class="v">{uptime_h:.1f}h</div></div>
  <div class="stat"><div class="lbl">Kill-switch</div><div class="v" style="color:{'#ef4444' if snap.kill_switch_engaged else '#22c55e'}">{('ENGAGED' if snap.kill_switch_engaged else 'ARMED')}</div></div>
</div>
<hr/>
<p>This is the <b>agent API</b>, not the dashboard. The Bloomberg-style
operator UI lives in a separate Railway service. Useful endpoints here:</p>
<ul>
  <li><a href="/health">/health</a> — liveness probe</li>
  <li><a href="/api/state">/api/state</a> — full snapshot JSON</li>
  <li><a href="/api/settings">/api/settings</a> — effective config</li>
  <li><a href="/api/stream">/api/stream</a> — Server-Sent Events feed</li>
  <li><a href="/api/docs">/api/docs</a> — Swagger / OpenAPI docs</li>
</ul>
<p>To open the dashboard, deploy the <code>web</code> service (Next.js,
root directory <code>web/</code>) with <code>NEXT_PUBLIC_API_URL</code> set to this URL.
See <a href="https://github.com/FrhnYldzl/Poly-Meridian/blob/main/docs/railway-deploy.md">docs/railway-deploy.md</a>.</p>
</body></html>"""

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
