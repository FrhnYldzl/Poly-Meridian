"""Agent entrypoint — asyncio event loop.

Phase 2 wiring:
  - Logging + Prometheus + /health
  - DB + Redis (graceful degrade)
  - Periodic Gamma sync → markets table
  - GDELT polling → news_articles
  - **Pipeline**: ArbitrageStrategy → Aggregator → RiskPolicy → PaperExecutor
  - Paper Ledger starting at $100K virtual NAV
  - WS subscription deliberately NOT enabled by default — pipeline runs on
    Gamma metadata + per-tick REST book snapshots so the agent is useful
    even before WS is wired (Phase 3 turns WS on for sub-second loop).
"""
from __future__ import annotations

import asyncio
import signal
from decimal import Decimal
from typing import Any

import structlog
import yaml
from prometheus_client import Counter, Gauge, start_http_server

from poly_meridian.execution import PaperExecutor
from poly_meridian.ingestion import GammaClient, GdeltNewsSource
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.ingestion.clob_client import ClobClient
from poly_meridian.ingestion.normalize import (
    book_snapshot_to_domain,
    gamma_market_to_domain,
    gamma_market_to_row,
)
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.pipeline import Pipeline
from poly_meridian.portfolio import Ledger, snapshot
from poly_meridian.risk import DefaultRiskPolicy, RiskLimits
from poly_meridian.settings import get_settings
from poly_meridian.storage import close_cache, close_db, get_cache, get_db
from poly_meridian.storage.writers import insert_news_article, upsert_markets
from poly_meridian.strategies import ArbitrageStrategy, SignalAggregator

GAMMA_SYNC_INTERVAL_SEC = 300
PIPELINE_TICK_INTERVAL_SEC = 10
MARKET_SAMPLE_SIZE = 20  # tick this many active markets per cycle (Phase 2 scope)

PM_MARKETS_TOTAL = Gauge("pm_markets_total", "active markets known to the agent")
PM_NEWS_INGESTED = Counter("pm_news_ingested_total", "news articles ingested")
PM_NAV_USD = Gauge("pm_nav_total", "current NAV in USD (paper)")
PM_OPEN_POSITIONS = Gauge("pm_position_count", "open positions")
PM_KILL_SWITCH = Gauge("pm_kill_switch_engaged", "1 if kill-switch engaged")


def _load_strategy_config() -> dict[str, Any]:
    cfg = get_settings().config_dir / "strategies" / "arbitrage.yaml"
    try:
        return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _load_risk_limits() -> RiskLimits:
    cfg = get_settings().config_dir / "risk.yaml"
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return RiskLimits()
    r = (data or {}).get("risk", {})
    return RiskLimits(
        kelly_fraction=float(r.get("kelly_fraction", 0.25)),
        max_position_pct_of_bankroll=float(r.get("max_position_pct_of_bankroll", 0.05)),
        max_exposure_per_category_pct=float(r.get("max_exposure_per_category_pct", 0.30)),
        max_total_exposure_pct=float(r.get("max_total_exposure_pct", 0.80)),
        daily_max_loss_pct=float(r.get("daily_max_loss_pct", 0.05)),
        weekly_max_loss_pct=float(r.get("weekly_max_loss_pct", 0.10)),
        max_concentration_single_event_pct=float(
            r.get("max_concentration_single_event_pct", 0.10)
        ),
        max_open_positions=int(r.get("max_open_positions", 50)),
        min_market_liquidity_usd=float(r.get("min_market_liquidity_usd", 10_000)),
        max_position_pct_of_market_volume=float(
            r.get("max_position_pct_of_market_volume", 0.05)
        ),
    )


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


async def _gamma_sync_loop(stop: asyncio.Event, log: Any) -> list[dict[str, Any]]:
    """Returns the most recent raw Gamma rows so the pipeline can use them."""
    latest: list[dict[str, Any]] = []
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
                try:
                    db = await get_db()
                    await upsert_markets(db, rows)
                except Exception as exc:
                    log.warning("gamma_sync.persist_skip", error=str(exc))
                PM_MARKETS_TOTAL.set(len(rows))
                latest = raw
                log.info("gamma_sync.done", n=len(rows))
        except Exception as exc:
            log.warning("gamma_sync.error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=GAMMA_SYNC_INTERVAL_SEC)
            return latest
        except asyncio.TimeoutError:
            continue
    return latest


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


async def _pipeline_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    market_cache: dict[str, Any],
    log: Any,
) -> None:
    """Drive the pipeline: pull REST book snapshots for sampled markets,
    update local books, run the strategy/risk/executor chain."""
    clob = ClobClient()
    await clob.start()
    try:
        while not stop.is_set():
            markets = market_cache.get("markets", [])
            if not markets:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=PIPELINE_TICK_INTERVAL_SEC)
                    return
                except asyncio.TimeoutError:
                    continue

            sample = markets[:MARKET_SAMPLE_SIZE]
            for raw in sample:
                m = gamma_market_to_domain(raw)
                if m is None:
                    continue
                pipeline.register_market(m)

                # Pull both legs' book snapshots — required for arb detection.
                try:
                    yes_snap = await clob.book_snapshot(m.yes_token_id)
                    no_snap = await clob.book_snapshot(m.no_token_id)
                except Exception as exc:
                    log.debug("pipeline.book_fetch_error", error=str(exc), token=m.yes_token_id)
                    continue

                for snap, token in ((yes_snap, m.yes_token_id), (no_snap, m.no_token_id)):
                    ob = book_snapshot_to_domain({**snap, "asset_id": token})
                    if ob is None:
                        continue
                    book = LocalBook(token_id=token)
                    book.apply_snapshot({
                        "bids": [{"price": str(lv.price), "size": str(lv.size)} for lv in ob.bids],
                        "asks": [{"price": str(lv.price), "size": str(lv.size)} for lv in ob.asks],
                    })
                    pipeline.attach_book(token, book)

                try:
                    order = await pipeline.tick(m)
                    if order is not None:
                        log.info(
                            "pipeline.order",
                            order_id=order.order_id,
                            status=str(order.status),
                            side=str(order.side),
                            token_id=order.token_id,
                        )
                except Exception as exc:
                    log.warning("pipeline.tick_error", error=str(exc), token=m.yes_token_id)

            # Periodic resting-order reconcile + metric refresh.
            await pipeline.executor.reconcile()
            snap = snapshot(pipeline.ledger)
            PM_NAV_USD.set(float(snap.nav_usd))
            PM_OPEN_POSITIONS.set(snap.open_position_count)
            PM_KILL_SWITCH.set(1 if pipeline.risk.is_kill_switch_engaged() else 0)

            try:
                await asyncio.wait_for(stop.wait(), timeout=PIPELINE_TICK_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        await clob.stop()


def _build_pipeline() -> Pipeline:
    strat_cfg = _load_strategy_config()
    limits = _load_risk_limits()
    strategy = ArbitrageStrategy(strat_cfg)
    aggregator = SignalAggregator(max_size_pct_per_position=limits.max_position_pct_of_bankroll)
    ledger = Ledger(starting_cash_usd=Decimal("100000"))  # paper $100K
    executor = PaperExecutor()
    risk = DefaultRiskPolicy(strategy_name=strategy.name, limits=limits)

    pipeline = Pipeline(
        strategy=strategy,
        aggregator=aggregator,
        risk=risk,
        executor=executor,
        ledger=ledger,
    )
    executor._on_fill = pipeline.on_fill  # bind callback now that pipeline exists
    return pipeline


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

    pipeline = _build_pipeline()
    market_cache: dict[str, Any] = {"markets": []}

    async def _gamma_with_cache() -> None:
        rows = await _gamma_sync_loop(stop_event, log)
        market_cache["markets"] = rows

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_serve_health(settings.prometheus_port), name="health"),
        asyncio.create_task(_gamma_with_cache(), name="gamma_sync"),
        asyncio.create_task(_news_loop(stop_event, log), name="news"),
        asyncio.create_task(
            _pipeline_loop(stop_event, pipeline, market_cache, log), name="pipeline"
        ),
    ]

    log.info(
        "agent.ready",
        health_port=settings.prometheus_port,
        db_ok=db_ok,
        cache_ok=cache_ok,
        starting_nav_usd=float(pipeline.ledger.cash),
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
