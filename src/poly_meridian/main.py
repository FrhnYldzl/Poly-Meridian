"""Agent entrypoint — asyncio event loop.

Phase 1 wiring:
  - Configure logging
  - Connect to DB + Redis
  - Periodically sync Gamma markets into storage
  - Subscribe a small CLOB WS sample to validate the data path end-to-end
  - Periodically poll GDELT
  - Expose /health for Railway/Docker probes

No strategies wired yet — that's Phase 2.
"""
from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, start_http_server

from poly_meridian.ingestion import GammaClient, GdeltNewsSource
from poly_meridian.ingestion.normalize import gamma_market_to_row
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.settings import get_settings
from poly_meridian.storage import close_cache, close_db, get_cache, get_db
from poly_meridian.storage.writers import insert_news_article, upsert_markets

GAMMA_SYNC_INTERVAL_SEC = 300
PM_MARKETS_TOTAL = Gauge("pm_markets_total", "active markets known to the agent")
PM_NEWS_INGESTED = Counter("pm_news_ingested_total", "news articles ingested")


async def _serve_health(port: int) -> None:
    from http import HTTPStatus
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, server.serve_forever)


async def _gamma_sync_loop(stop: asyncio.Event, log: Any) -> None:
    while not stop.is_set():
        try:
            async with GammaClient() as g:
                raw = await g.iter_active_markets()
            rows: list[dict[str, Any]] = []
            for r in raw:
                row = gamma_market_to_row(r)
                if row is not None:
                    rows.append(row)
            if rows:
                db = await get_db()
                await upsert_markets(db, rows)
                PM_MARKETS_TOTAL.set(len(rows))
                log.info("gamma_sync.done", n=len(rows))
        except Exception as exc:
            log.warning("gamma_sync.error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=GAMMA_SYNC_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            continue


async def _news_loop(stop: asyncio.Event, log: Any) -> None:
    src = GdeltNewsSource()
    await src.start()
    try:
        async for evt in src.events():
            if stop.is_set():
                break
            if evt.get("type") != "news_article":
                continue
            payload = evt.get("payload", {})
            try:
                db = await get_db()
                await insert_news_article(
                    db,
                    article_id=payload["article_id"],
                    ts=evt["ts"],
                    source=payload.get("source"),
                    title=payload.get("title"),
                    body=None,
                    url=payload.get("url"),
                )
                PM_NEWS_INGESTED.inc()
            except Exception as exc:
                log.warning("news.write_error", error=str(exc))
    finally:
        await src.stop()


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = structlog.get_logger("poly_meridian.main")
    log.info("agent.boot", mode=settings.mode, version="0.1.0")

    try:
        start_http_server(settings.prometheus_port + 1)
    except OSError:
        log.warning("prometheus.bind_failed", port=settings.prometheus_port + 1)

    stop_event = asyncio.Event()

    def _on_signal(*_: object) -> None:
        log.info("agent.shutdown_requested")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _on_signal())

    # Eagerly connect infra so failures surface at boot, not on first use.
    db_ok = cache_ok = False
    try:
        await get_db()
        db_ok = True
    except Exception as exc:
        log.warning("db.boot_skip", error=str(exc))
    try:
        await get_cache()
        cache_ok = True
    except Exception as exc:
        log.warning("cache.boot_skip", error=str(exc))

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_serve_health(settings.prometheus_port), name="health"),
    ]
    if db_ok:
        tasks.append(asyncio.create_task(_gamma_sync_loop(stop_event, log), name="gamma_sync"))
        tasks.append(asyncio.create_task(_news_loop(stop_event, log), name="news_loop"))

    log.info(
        "agent.ready",
        health_port=settings.prometheus_port,
        db_ok=db_ok,
        cache_ok=cache_ok,
    )

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    await close_db()
    await close_cache()
    log.info("agent.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
