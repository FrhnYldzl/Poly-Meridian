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

from poly_meridian.alerts import post_slack_alert, slack_alert_async
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
from poly_meridian.risk.trade_metrics import compute_trade_metrics
from poly_meridian.sentiment import (
    ClaudeSentimentScorer,
    GeminiSentimentScorer,
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
    ClusterStateBuilder,
    SentimentStrategy,
    SignalAggregator,
    SmartMoneyStrategy,
    StatQuantStrategy,
)

GAMMA_SYNC_INTERVAL_SEC = 300
NEWS_PROCESS_INTERVAL_SEC = 180
PIPELINE_TICK_INTERVAL_SEC = 5
# Polymarket CLOB WS allows up to ~100 asset_ids per connection.
# 50 markets × 2 outcomes = 100 token IDs — at the safe ceiling on a single
# connection. Raising further would require multiplexing across connections.
WS_SAMPLE_SIZE = 50
# Per-category cap when picking markets to subscribe to — without this, one
# hot category (Politics during elections, Crypto during halving, etc.)
# eats every WS slot and the agent goes blind on everything else.
WS_MAX_PER_CATEGORY = 12

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


def _pick_markets_for_ws(
    markets: list[dict[str, Any]],
    *,
    sample_size: int,
    max_per_category: int,
) -> list[dict[str, Any]]:
    """Rank markets by liquidity DESC, cap per category, return top N.

    Without the per-category cap a single hot category (Politics around
    elections, Crypto around halvings) eats every WS subscription slot and
    the agent goes blind on everything else. With the cap we get coverage
    proportional to how active each category is.
    """
    if not markets:
        return []

    def _liq(r: dict[str, Any]) -> float:
        v = r.get("liquidityNum") or r.get("liquidity") or 0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(markets, key=_liq, reverse=True)
    picked: list[dict[str, Any]] = []
    per_cat: dict[str, int] = {}
    for r in ranked:
        if len(picked) >= sample_size:
            break
        cat = (r.get("category") or r.get("sub_category") or "Other").strip() or "Other"
        if per_cat.get(cat, 0) >= max_per_category:
            continue
        picked.append(r)
        per_cat[cat] = per_cat.get(cat, 0) + 1

    # If we still have slots after the cap (very narrow universe), fill with
    # the highest-liquidity leftovers ignoring the cap.
    if len(picked) < sample_size:
        ids_in = {id(r) for r in picked}
        for r in ranked:
            if id(r) in ids_in:
                continue
            picked.append(r)
            if len(picked) >= sample_size:
                break
    return picked


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


def _counter_total(c: Counter) -> int:
    """Sum all label-permutations of a Prometheus Counter into one int.
    Works for both labeled (multiple samples) and unlabeled counters.
    Stable across prometheus_client versions; uses public .collect() API."""
    try:
        total = 0.0
        for fam in c.collect():
            for s in fam.samples:
                if s.name.endswith("_total"):
                    total += s.value
            break
        return int(total)
    except Exception:
        return 0


async def _broker_refresh_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    broker: AgentStateBroker,
    news_proc: NewsProcessor | None = None,
) -> None:
    """Pulls portfolio + kill-switch state every 5s and pushes to the broker."""
    # Lazy-import counter refs — they live in different modules.
    from poly_meridian.sentiment.news_processor import (
        PM_NEWS_PROCESSED,
        PM_NEWS_SIGNAL_EMITTED,
    )

    scorer_kind: str | None = None
    if news_proc is not None:
        # type(scorer).__name__ → "ClaudeSentimentScorer" → "claude"
        cls_name = type(news_proc._scorer).__name__  # type: ignore[reportPrivateUsage]
        scorer_kind = cls_name.replace("SentimentScorer", "").lower() or None

    while not stop.is_set():
        try:
            snap = snapshot(pipeline.ledger)
            # Build a fast lookup: token_id -> most-recent BUY entry in the
            # ledger. This recovers strategy attribution for positions that
            # were opened before broker.push_order was wired (Phase B.1) —
            # the ledger keeps every fill in memory regardless.
            entry_by_token: dict[str, dict[str, Any]] = {}
            for entry in reversed(pipeline.ledger.entries()):
                if entry.token_id in entry_by_token:
                    continue
                if entry.qty <= 0:  # SELL fills have negative signed qty
                    continue
                entry_by_token[entry.token_id] = {
                    "strategy": entry.strategy,
                    "entry_price": float(entry.price),
                    "entry_ts": entry.ts.isoformat(),
                }
            # Per-position risk/reward (uses avg_cost as the entry price so
            # max_loss / max_gain reflect *cumulative* notional, not just the
            # last fill). Edge defaults to 0 — once the order is in the books
            # we don't have the strategy's original edge handy here.
            open_positions_out: list[dict[str, Any]] = []
            for p in pipeline.ledger.positions():
                tm = compute_trade_metrics(
                    entry_price=float(p.avg_cost) if p.avg_cost else None,
                    size_units=abs(float(p.qty)),
                    edge=0.0,
                )
                open_positions_out.append({
                    "token_id": p.token_id,
                    "qty": float(p.qty),
                    "avg_cost": float(p.avg_cost),
                    "last_mark": float(p.last_mark),
                    "unrealized_pnl": float(p.qty * (p.last_mark - p.avg_cost)),
                    "entry": entry_by_token.get(p.token_id),
                    "trade_metrics": tm.asdict() if tm is not None else None,
                })
            broker.update_portfolio(
                nav_usd=snap.nav_usd,
                cash_usd=snap.cash_usd,
                open_position_count=snap.open_position_count,
                daily_pnl_pct=snap.daily_pnl_pct,
                total_exposure_pct=snap.total_exposure_pct,
                open_positions=open_positions_out,
            )
            broker.update_kill_switch(
                engaged=pipeline.risk.is_kill_switch_engaged(),
                reason=str(pipeline.risk.kill_switch.reason) if pipeline.risk.kill_switch.engaged else None,
            )
            # News funnel telemetry — articles fetched → processed → signals.
            # Sourced from in-process Prometheus counters so the dashboard
            # shows the *real* pipeline state without log access.
            broker.update_news_stats(
                ingested_total=_counter_total(PM_NEWS_INGESTED),
                processed_total=_counter_total(PM_NEWS_PROCESSED),
                signals_emitted_total=_counter_total(PM_NEWS_SIGNAL_EMITTED),
                matcher_mode=news_proc.mode if news_proc is not None else None,
                scorer_kind=scorer_kind,
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
    cluster_builder: ClusterStateBuilder | None = None,
    broker: AgentStateBroker | None = None,
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
                            # Also refresh in-memory matcher (no-op if not configured).
                            if news_proc._inmem_matcher is not None:  # type: ignore[reportPrivateUsage]
                                await news_proc._inmem_matcher.refresh_markets(raw)  # type: ignore[reportPrivateUsage]
                        except Exception as exc:
                            log.warning("gamma_sync.embed_markets_failed", error=str(exc))
                except Exception as exc:
                    log.warning("gamma_sync.persist_skip", error=str(exc))
                PM_MARKETS_TOTAL.set(len(rows))
                market_cache["markets"] = raw
                # Category breakdown for the operator dashboard. Empty /
                # uncategorized markets fall into "Other" so totals match.
                if broker is not None:
                    cat_counts: dict[str, int] = {}
                    for r in raw:
                        cat = (r.get("category") or r.get("sub_category") or "Other").strip() or "Other"
                        cat_counts[cat] = cat_counts.get(cat, 0) + 1
                    broker.update_market_coverage(
                        markets_by_category=cat_counts,
                        markets_active_total=len(raw),
                        ws_subscribed_total=min(WS_SAMPLE_SIZE, len(raw)),
                    )
                # Register token→condition mappings into the cluster builder
                # so when on-chain CTF transfers arrive, we can route them
                # to the right condition. Safe to call every sync — idempotent.
                if cluster_builder is not None:
                    registered = 0
                    for r in raw:
                        m = gamma_market_to_domain(r)
                        if m is None:
                            continue
                        cluster_builder.register_token_to_condition(
                            token_id=m.yes_token_id,
                            condition_id=m.condition_id,
                            direction="YES",
                        )
                        cluster_builder.register_token_to_condition(
                            token_id=m.no_token_id,
                            condition_id=m.condition_id,
                            direction="NO",
                        )
                        registered += 2
                    log.info("cluster_builder.tokens_registered", n=registered)
                log.info("gamma_sync.done", n=len(rows))
        except Exception as exc:
            log.warning("gamma_sync.error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=GAMMA_SYNC_INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            continue


async def _news_ingest_loop(
    stop: asyncio.Event,
    market_cache: dict[str, Any],
    log: Any,
) -> None:
    """Pulls news from GDELT and writes to news_articles. Uses dynamic
    per-category queries built from the active market cache when available."""
    src = GdeltNewsSource()
    await src.start()
    # Build dynamic queries from currently-active markets if we have them yet.
    try:
        from poly_meridian.sentiment.market_query_builder import build_queries_from_markets
        dynamic = build_queries_from_markets(market_cache.get("markets", []))
        if dynamic:
            src.set_dynamic_queries(dynamic)
            log.info("news.dynamic_queries", n=len(dynamic))
    except Exception as exc:
        log.debug("news.dynamic_query_build_failed", error=str(exc))
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

            # Smart sampling: rank by liquidity DESC, but cap per category so
            # one hot category doesn't eat every WS slot. Result is N markets
            # spread across categories with the most active books in each.
            picked = _pick_markets_for_ws(
                markets,
                sample_size=WS_SAMPLE_SIZE,
                max_per_category=WS_MAX_PER_CATEGORY,
            )
            sampled = []
            for raw in picked:
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
            n_strategies = sum(
                1 for s in (
                    pipeline.arbitrage, pipeline.sentiment, pipeline.smart_money,
                    pipeline.stat_quant,
                ) if s and getattr(s, "enabled", False)
            )
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
    sq_cfg = _load_yaml("strategies/stat_quant.yaml")
    limits = _load_risk_limits()
    settings = get_settings()

    arbitrage = ArbitrageStrategy(arb_cfg)
    sentiment = SentimentStrategy(sent_cfg)
    smart_money = SmartMoneyStrategy(sm_cfg)
    stat_quant = StatQuantStrategy(sq_cfg)

    aggregator = SignalAggregator(max_size_pct_per_position=limits.max_position_pct_of_bankroll)
    starting_nav = Decimal("100000") if str(settings.mode) == "paper" else Decimal("500")
    ledger = Ledger(starting_cash_usd=starting_nav)
    executor = _build_executor(str(settings.mode))
    risk = DefaultRiskPolicy(strategy_name="poly_meridian", limits=limits)

    pipeline = Pipeline(
        arbitrage=arbitrage,
        sentiment=sentiment,
        smart_money=smart_money,
        stat_quant=stat_quant,
        aggregator=aggregator,
        risk=risk,
        executor=executor,
        ledger=ledger,
    )
    executor._on_fill = pipeline.on_fill  # type: ignore[attr-defined]

    # Smart-money cluster builder — aggregates on-chain CTF transfers into
    # per-condition cluster states that SmartMoneyStrategy consumes. The
    # builder stays warm even before an on-chain event source is wired:
    # gamma_sync registers token→condition mappings so once events flow,
    # cluster snapshots fire immediately. .start(events) gets called when
    # we add the Alchemy/RPC feed.
    cluster_builder = ClusterStateBuilder()
    if smart_money is not None:
        cluster_builder.attach_to_strategy(smart_money)
    pipeline.cluster_builder = cluster_builder  # type: ignore[attr-defined]

    # Build sentiment processor based on available API keys.
    # Scorer priority: Anthropic Claude > Google Gemini > heuristic keyword.
    # Matching: OpenAI embeddings (vector mode) when available, else Postgres
    # ILIKE keyword fallback. Either path activates sentiment_enabled.
    s = get_settings()
    has_openai = bool(s.openai_api_key.get_secret_value())
    has_anthropic = bool(s.anthropic_api_key.get_secret_value())
    has_gemini = bool(s.gemini_api_key.get_secret_value())
    if has_openai or has_anthropic or has_gemini:
        try:
            embeddings = OpenAIEmbeddings() if has_openai else None
            if has_anthropic:
                scorer = ClaudeSentimentScorer()
            elif has_gemini:
                scorer = GeminiSentimentScorer()
            else:
                scorer = HeuristicSentimentScorer()

            # When OpenAI is available but pgvector isn't (Railway stock
            # Postgres), use in-memory cosine matcher — no DB extension needed.
            inmem_matcher = None
            if embeddings is not None:
                from poly_meridian.sentiment.inmem_matcher import InMemoryMatcher
                inmem_matcher = InMemoryMatcher(embeddings)

            news_proc = NewsProcessor(
                embeddings=embeddings, scorer=scorer, inmem_matcher=inmem_matcher,
            )
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
        db = await get_db()
        # Auto-bootstrap schema so first-time deploys don't need psql.
        # Idempotent — every statement uses IF NOT EXISTS.
        try:
            from poly_meridian.storage.schema import initialize_schema
            await initialize_schema(db)
        except Exception as exc:
            log.warning("schema.bootstrap_failed", error=str(exc))
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
            pipeline.stat_quant,
        ) if s is not None and getattr(s, "enabled", False)
    ])
    broker.update_infra(
        db_ok=db_ok,
        cache_ok=cache_ok,
        sentiment_enabled=news_proc is not None,
    )
    # Expose broker on pipeline so its hooks can push events.
    pipeline.broker = broker  # type: ignore[attr-defined]

    # ---- Slack alert drill (Phase A.3) ----
    # 4 alert types fired by the broker (no-ops when SLACK_WEBHOOK_URL unset):
    #   • boot — agent ready
    #   • kill_switch — engage/disengage transitions
    #   • first_signal — first paper signal of session
    #   • first_fill — first paper order of session
    def _alert_first_signal(sig: dict[str, Any]) -> None:
        strat = sig.get("strategy", "?")
        edge = sig.get("edge", 0)
        cid = (sig.get("condition_id") or "?")[:10]
        post_slack_alert(
            f"first paper signal · {strat} · edge={edge:.3f} · cid={cid}…",
            level="signal",
        )

    def _alert_first_order(order: dict[str, Any]) -> None:
        side = order.get("side", "?")
        token = (order.get("token_id") or "?")[:12]
        price = order.get("price")
        size = order.get("size")
        post_slack_alert(
            f"first paper fill · {side} {size} @ {price} · token={token}…",
            level="fill",
        )

    def _alert_kill_switch(engaged: bool, reason: str | None) -> None:
        if engaged:
            post_slack_alert(
                f"KILL-SWITCH ENGAGED · {reason or 'no reason'}",
                level="error",
            )
        else:
            post_slack_alert("kill-switch disengaged · trading resumed", level="warn")

    broker.set_first_signal_hook(_alert_first_signal)
    broker.set_first_order_hook(_alert_first_order)
    broker.set_kill_switch_hook(_alert_kill_switch)

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_serve_api(settings.prometheus_port, broker), name="api"),
        asyncio.create_task(
            _gamma_sync_loop(
                stop_event, market_cache, news_proc, log,
                getattr(pipeline, "cluster_builder", None),
                broker,
            ),
            name="gamma_sync",
        ),
        asyncio.create_task(_news_ingest_loop(stop_event, market_cache, log), name="news_ingest"),
        asyncio.create_task(
            _pipeline_loop(stop_event, pipeline, market_cache, log), name="pipeline"
        ),
        asyncio.create_task(
            _broker_refresh_loop(stop_event, pipeline, broker, news_proc), name="broker_refresh"
        ),
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
    # Boot alert — fire-and-forget Slack post. No-op if SLACK_WEBHOOK_URL unset.
    try:
        await slack_alert_async(
            f"boot · mode={settings.mode} · "
            f"NAV=${float(pipeline.ledger.cash):,.0f} · "
            f"sentiment={'on' if news_proc is not None else 'off'}",
            level="info",
        )
    except Exception:
        pass

    await stop_event.wait()

    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    # Shutdown alert.
    try:
        await slack_alert_async("agent shutdown · clean stop", level="warn")
    except Exception:
        pass

    await close_db()
    await close_cache()
    log.info("agent.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
