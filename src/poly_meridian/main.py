"""Agent entrypoint — asyncio event loop.

Phase 3 wiring:
  - Logging + Prometheus + /health
  - DB + Redis (graceful degrade)
  - Gamma sync → markets table + market_embeddings (lazy)
  - GDELT polling → news_articles
  - **News processor**: embeds + scores via Claude → news_signals
  - **WS subscription** to sampled markets — drives sub-second pipeline ticks
  - **Pipeline**: arb + sentiment + smart_money → Aggregator → RiskPolicy → PaperExecutor
  - Smart-money cluster builder (Phase 3 keeps cluster state in memory; on-chain
    feed populates it as events arrive)
  - Paper Ledger starting at $100K virtual NAV
"""
from __future__ import annotations

import asyncio
import signal
from decimal import Decimal
from typing import Any

import structlog
import yaml
from prometheus_client import Counter, Gauge, start_http_server

from poly_meridian.api import AgentStateBroker, build_app
from poly_meridian.execution import PaperExecutor
from poly_meridian.ingestion import GammaClient, GdeltNewsSource
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.ingestion.clob_ws import ClobWebsocketSource
from poly_meridian.ingestion.normalize import (
    gamma_market_to_domain,
    gamma_market_to_row,
)
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.pipeline import Pipeline
from poly_meridian.portfolio import Ledger, snapshot
from poly_meridian.risk import DefaultRiskPolicy, RiskLimits
from poly_meridian.sentiment import (
    ClaudeSentimentScorer,
    HeuristicSentimentScorer,
    OpenAIEmbeddings,
)
from poly_meridian.sentiment.news_processor import NewsProcessor
from poly_meridian.settings import get_settings
from poly_meridian.storage import close_cache, close_db, get_cache, get_db
from poly_meridian.storage.writers import (
    fetch_recent_news_signals,
    insert_news_article,
    upsert_markets,
)
from poly_meridian.strategies import (
    ArbitrageStrategy,
    SentimentStrategy,
    SignalAggregator,
    SmartMoneyStrategy,
)

GAMMA_SYNC_INTERVAL_SEC = 300
NEWS_PROCESS_INTERVAL_SEC = 180
PIPELINE_TICK_INTERVAL_SEC = 5
WS_SAMPLE_SIZE = 40  # markets subscribed via WS

PM_MARKETS_TOTAL = Gauge("pm_markets_total", "active markets known to the agent")
PM_NEWS_INGESTED = Counter("pm_news_ingested_total", "news articles ingested")
PM_NAV_USD = Gauge("pm_nav_total", "current NAV in USD (paper)")
PM_OPEN_POSITIONS = Gauge("pm_position_count", "open positions")
PM_KILL_SWITCH = Gauge("pm_kill_switch_engaged", "1 if kill-switch engaged")
PM_WS_BOOKS = Gauge("pm_ws_books_tracked", "WS books actively maintained")


def _load_yaml(path: str) -> dict[str, Any]:
    cfg = get_settings().config_dir / path
    try:
        return yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def _load_risk_limits() -> RiskLimits:
    data = _load_yaml("risk.yaml")
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


def _load_smart_wallets() -> list[str]:
    data = _load_yaml("smart_wallets.yaml")
    wallets = data.get("wallets", []) if isinstance(data, dict) else []
    return [w for w in wallets if isinstance(w, str) and w.startswith("0x")]


async def _bootstrap_books(
    pipeline: Pipeline,
    markets: list[Any],
    log: Any,
) -> None:
    """Fetch initial book snapshots for sampled markets via CLOB REST and
    populate the local books — WS subscribe only streams deltas thereafter."""
    from poly_meridian.ingestion.clob_client import ClobClient
    from poly_meridian.ingestion.normalize import book_snapshot_to_domain

    clob = ClobClient()
    await clob.start()
    bootstrapped = 0
    try:
        for m in markets:
            for token_id in (m.yes_token_id, m.no_token_id):
                try:
                    snap = await clob.book_snapshot(token_id)
                except Exception:
                    continue
                ob = book_snapshot_to_domain({**snap, "asset_id": token_id})
                if ob is None:
                    continue
                book = pipeline._books.get(token_id)  # type: ignore[reportPrivateUsage]
                if book is None:
                    continue
                book.apply_snapshot({
                    "bids": [{"price": str(lv.price), "size": str(lv.size)} for lv in ob.bids],
                    "asks": [{"price": str(lv.price), "size": str(lv.size)} for lv in ob.asks],
                })
                bootstrapped += 1
    finally:
        await clob.stop()
    log.info("pipeline.books_bootstrapped", n=bootstrapped)


async def _broker_refresh_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    broker: AgentStateBroker,
) -> None:
    """Pulls portfolio + kill-switch state every 5s and pushes to the broker."""
    while not stop.is_set():
        try:
            snap = snapshot(pipeline.ledger)
            broker.update_portfolio(
                nav_usd=snap.nav_usd,
                cash_usd=snap.cash_usd,
                open_position_count=snap.open_position_count,
                daily_pnl_pct=snap.daily_pnl_pct,
                total_exposure_pct=snap.total_exposure_pct,
                open_positions=[
                    {
                        "token_id": p.token_id,
                        "qty": float(p.qty),
                        "avg_cost": float(p.avg_cost),
                        "last_mark": float(p.last_mark),
                        "unrealized_pnl": float(p.qty * (p.last_mark - p.avg_cost)),
                    }
                    for p in pipeline.ledger.positions()
                ],
            )
            broker.update_kill_switch(
                engaged=pipeline.risk.is_kill_switch_engaged(),
                reason=str(pipeline.risk.kill_switch.reason) if pipeline.risk.kill_switch.engaged else None,
            )
            # Push full snapshot so the UI's SSE subscriber refreshes ticks /
            # markets / NAV / counters every 5s without re-fetching REST.
            broker.emit_snapshot()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            continue


async def _serve_api(port: int, broker: AgentStateBroker) -> None:
    """FastAPI app serving /health + /api/state + /api/stream (SSE)."""
    import uvicorn

    app = build_app(broker)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=port,    # noqa: S104
        log_level="warning", access_log=False, loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def _gamma_sync_loop(
    stop: asyncio.Event,
    market_cache: dict[str, Any],
    news_proc: NewsProcessor | None,
    log: Any,
) -> None:
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
                    if news_proc is not None:
                        try:
                            await news_proc.embed_markets_if_stale(db, raw)
                        except Exception as exc:
                            log.warning("gamma_sync.embed_markets_failed", error=str(exc))
                except Exception as exc:
                    log.warning("gamma_sync.persist_skip", error=str(exc))
                PM_MARKETS_TOTAL.set(len(rows))
                market_cache["markets"] = raw
                log.info("gamma_sync.done", n=len(rows))
        except Exception as exc:
            log.warning("gamma_sync.error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=GAMMA_SYNC_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            continue


async def _news_ingest_loop(stop: asyncio.Event, log: Any) -> None:
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


async def _news_process_loop(stop: asyncio.Event, proc: NewsProcessor, log: Any) -> None:
    while not stop.is_set():
        try:
            db = await get_db()
            await proc.process_unprocessed(db, batch=25)
        except Exception as exc:
            log.warning("news_process.error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=NEWS_PROCESS_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            continue


async def _pipeline_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    market_cache: dict[str, Any],
    log: Any,
) -> None:
    """Phase 3 pipeline: drive ticks off WS-maintained local books."""
    ws_source: ClobWebsocketSource | None = None
    subscribed_tokens: set[str] = set()
    try:
        while not stop.is_set():
            markets = market_cache.get("markets", [])
            if not markets:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=PIPELINE_TICK_INTERVAL_SEC)
                    return
                except asyncio.TimeoutError:
                    continue

            # Sample markets for WS subscription. Re-subscribe whenever the
            # active set changes meaningfully (Phase 3: simple resubscribe
            # every gamma cycle).
            sampled = []
            for raw in markets[:WS_SAMPLE_SIZE]:
                m = gamma_market_to_domain(raw)
                if m is None:
                    continue
                sampled.append(m)

            new_tokens: set[str] = set()
            for m in sampled:
                new_tokens.add(m.yes_token_id)
                new_tokens.add(m.no_token_id)
                pipeline.register_market(m)

            if new_tokens != subscribed_tokens:
                if ws_source is not None:
                    await ws_source.stop()
                ws_source = ClobWebsocketSource(asset_ids=sorted(new_tokens))
                await ws_source.start()
                subscribed_tokens = new_tokens
                # Attach books from the WS source.
                for tid in new_tokens:
                    book = ws_source.book(tid)
                    if book is not None:
                        pipeline.attach_book(tid, book)
                PM_WS_BOOKS.set(len(new_tokens))
                log.info("pipeline.ws_resubscribed", n=len(new_tokens))

                # Bootstrap initial book snapshots via REST — Polymarket WS
                # only streams *deltas* after subscribe, not initial state.
                # Without this, books stay empty until the next trade hits.
                await _bootstrap_books(pipeline, sampled, log)

            # Hydrate sentiment cache for sampled markets.
            try:
                db = await get_db()
                for m in sampled:
                    rows = await fetch_recent_news_signals(
                        db,
                        condition_id=m.condition_id,
                        window_sec=get_settings().sentiment_window_sec,
                    )
                    pipeline.attach_news_signals(m.condition_id, rows)
            except Exception as exc:
                log.debug("pipeline.sentiment_hydrate_skip", error=str(exc))

            # Tick each market once per cycle.
            n_strategies = sum(1 for s in (pipeline.arbitrage, pipeline.sentiment, pipeline.smart_money) if s and getattr(s, "enabled", False))
            broker = getattr(pipeline, "broker", None)
            if broker is not None:
                broker.update_markets_watched(len(sampled))
            for m in sampled:
                try:
                    order = await pipeline.tick(m)
                    if broker is not None:
                        broker.increment_pipeline_tick(strategies_evaluated=n_strategies)
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
        if ws_source is not None:
            await ws_source.stop()


def _build_executor(mode: str) -> Any:
    """Pick PaperExecutor or LiveExecutor based on MODE — the only place this branches."""
    from poly_meridian.domain import Mode
    if mode in (Mode.LIVE_CONSERVATIVE.value, Mode.LIVE_NORMAL.value, "live-conservative", "live-normal"):
        from poly_meridian.execution import LiveExecutor
        return LiveExecutor()
    return PaperExecutor()


def _build_pipeline_and_news_proc() -> tuple[Pipeline, NewsProcessor | None]:
    arb_cfg = _load_yaml("strategies/arbitrage.yaml")
    sent_cfg = _load_yaml("strategies/sentiment.yaml")
    sm_cfg = _load_yaml("strategies/smart_money.yaml")
    limits = _load_risk_limits()
    settings = get_settings()

    arbitrage = ArbitrageStrategy(arb_cfg)
    sentiment = SentimentStrategy(sent_cfg)
    smart_money = SmartMoneyStrategy(sm_cfg)

    aggregator = SignalAggregator(max_size_pct_per_position=limits.max_position_pct_of_bankroll)
    starting_nav = Decimal("100000") if str(settings.mode) == "paper" else Decimal("500")
    ledger = Ledger(starting_cash_usd=starting_nav)
    executor = _build_executor(str(settings.mode))
    risk = DefaultRiskPolicy(strategy_name="poly_meridian", limits=limits)

    pipeline = Pipeline(
        arbitrage=arbitrage,
        sentiment=sentiment,
        smart_money=smart_money,
        aggregator=aggregator,
        risk=risk,
        executor=executor,
        ledger=ledger,
    )
    executor._on_fill = pipeline.on_fill  # type: ignore[attr-defined]

    # Build sentiment processor only when both keys + a sensible model are set.
    s = get_settings()
    if s.openai_api_key.get_secret_value():
        try:
            embeddings = OpenAIEmbeddings()
            scorer = (
                ClaudeSentimentScorer()
                if s.anthropic_api_key.get_secret_value()
                else HeuristicSentimentScorer()
            )
            news_proc = NewsProcessor(embeddings=embeddings, scorer=scorer)
        except Exception:
            news_proc = None
    else:
        news_proc = None

    return pipeline, news_proc


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

    pipeline, news_proc = _build_pipeline_and_news_proc()
    market_cache: dict[str, Any] = {"markets": []}

    # Operator dashboard broker — agent pushes state here, UI subscribes via SSE.
    broker = AgentStateBroker()
    broker.update_mode(str(settings.mode))
    broker.update_strategies([
        s.name for s in (
            pipeline.arbitrage, pipeline.sentiment, pipeline.smart_money,
        ) if s is not None and getattr(s, "enabled", False)
    ])
    # Expose broker on pipeline so its hooks can push events.
    pipeline.broker = broker  # type: ignore[attr-defined]

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_serve_api(settings.prometheus_port, broker), name="api"),
        asyncio.create_task(
            _gamma_sync_loop(stop_event, market_cache, news_proc, log), name="gamma_sync"
        ),
        asyncio.create_task(_news_ingest_loop(stop_event, log), name="news_ingest"),
        asyncio.create_task(
            _pipeline_loop(stop_event, pipeline, market_cache, log), name="pipeline"
        ),
        asyncio.create_task(_broker_refresh_loop(stop_event, pipeline, broker), name="broker_refresh"),
    ]
    if news_proc is not None and db_ok:
        tasks.append(
            asyncio.create_task(
                _news_process_loop(stop_event, news_proc, log), name="news_process"
            )
        )

    log.info(
        "agent.ready",
        health_port=settings.prometheus_port,
        db_ok=db_ok,
        cache_ok=cache_ok,
        sentiment_enabled=news_proc is not None,
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
