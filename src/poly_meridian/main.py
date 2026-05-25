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
import json
import signal
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
import yaml
from prometheus_client import Counter, Gauge, start_http_server

from poly_meridian.alerts import post_slack_alert, slack_alert_async
from poly_meridian.api import AgentStateBroker, build_app
from poly_meridian.execution import PaperExecutor
from poly_meridian.execution.exit_monitor import ExitMonitor
from poly_meridian.execution.slippage_monitor import SlippageMonitor
from poly_meridian.ingestion import GammaClient, GdeltNewsSource
from poly_meridian.ingestion.book import LocalBook
from poly_meridian.ingestion.clob_ws import ClobWebsocketSource
from poly_meridian.ingestion.clob_user_ws import ClobUserChannel
from poly_meridian.ingestion.polymarket_trades import PolymarketTradesSource
from poly_meridian.ingestion.normalize import (
    build_event_category_map,
    extract_event_id,
    gamma_market_to_domain,
    gamma_market_to_row,
)
from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.pipeline import Pipeline
from poly_meridian.portfolio import Ledger, snapshot
from poly_meridian.portfolio.pnl import nav_usd as nav_usd_helper
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
    fetch_pnl_daily,
    fetch_pnl_per_strategy,
    fetch_positions,
    fetch_recent_news_signals,
    fetch_recent_orders,
    fetch_recent_strategy_signals,
    insert_ledger_entry,
    insert_news_article,
    insert_strategy_signal,
    insert_trade,
    upsert_markets,
    upsert_order,
    upsert_pnl_daily,
    upsert_position,
)
from poly_meridian.strategies import (
    ArbitrageStrategy,
    ClusterStateBuilder,
    FundamentalsStrategy,
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

    def _opt_float(key: str, default: float | None) -> float | None:
        v = r.get(key, default)
        return None if v is None else float(v)

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
        max_resolution_days=_opt_float("max_resolution_days", 45.0),
        min_resolution_days=_opt_float("min_resolution_days", 0.5),
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


def _counter_total_filtered(c: Counter, label: str, value_substr: str) -> int:
    """Sum samples whose `label` value contains `value_substr` (case-insensitive).
    Used for splitting PM_RISK_DECISION into accept vs reject buckets — risk
    decisions are emitted as RiskDecision.ACCEPT / .REJECT enums and we want
    a simple substring match instead of brittle exact-string comparison.
    """
    try:
        total = 0.0
        needle = value_substr.lower()
        for fam in c.collect():
            for s in fam.samples:
                if not s.name.endswith("_total"):
                    continue
                lbl_val = (s.labels or {}).get(label, "")
                if needle in str(lbl_val).lower():
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
    market_cache: dict[str, Any] | None = None,
) -> None:
    """Pulls portfolio + kill-switch state every 5s and pushes to the broker.

    market_cache is needed for position metadata (resolution dates, market
    questions, Polymarket URLs). Without it, the new resolution-aware
    columns silently regress to None — and the bare-except wrapping the
    refresh loop body would mask the NameError forever, leaving the
    snapshot's matcher_mode / scorer_kind / position-meta fields blank.
    """
    if market_cache is None:
        market_cache = {"markets": []}
    log = structlog.get_logger("poly_meridian.main.broker_refresh")
    # Lazy-import counter refs — they live in different modules.
    from poly_meridian.pipeline import (
        PM_ORDER_SUBMITTED,
        PM_RISK_DECISION,
        PM_SIGNAL_AGGREGATED,
        PM_SIGNAL_EMITTED,
    )
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
            # Phase N.5: pass day_start_nav for accurate daily_pnl_pct.
            day_start = getattr(pipeline, "day_start_nav", None)
            snap = snapshot(pipeline.ledger, day_start_nav=day_start)
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
            tok_to_cat = getattr(pipeline, "token_to_category", {}) or {}
            # Pull resolution dates AND market metadata (question, slug,
            # outcome side) from the market cache so positions can render
            # human-readable rows + a direct Polymarket link instead of
            # raw token IDs.
            tok_to_end: dict[str, datetime] = {}
            tok_to_meta: dict[str, dict[str, Any]] = {}
            for raw_m in market_cache.get("markets", []) or []:
                m_obj = gamma_market_to_domain(raw_m)
                if m_obj is None:
                    continue
                if m_obj.end_date_iso is not None:
                    tok_to_end[m_obj.yes_token_id] = m_obj.end_date_iso
                    tok_to_end[m_obj.no_token_id] = m_obj.end_date_iso
                # Gamma's "slug" is the per-market slug; "eventSlug" is the
                # parent event. Polymarket's market URL routes off the
                # event slug; fall back to market slug if event is missing.
                ev_slug = raw_m.get("eventSlug")
                if not ev_slug:
                    events_arr = raw_m.get("events")
                    if isinstance(events_arr, list) and events_arr:
                        first = events_arr[0]
                        if isinstance(first, dict):
                            ev_slug = first.get("slug")
                market_slug = raw_m.get("slug")
                question = m_obj.question
                # YES position vs NO position — which outcome we're long.
                tok_to_meta[m_obj.yes_token_id] = {
                    "question": question,
                    "outcome": "Yes",
                    "event_slug": ev_slug,
                    "market_slug": market_slug,
                    "condition_id": m_obj.condition_id,
                }
                tok_to_meta[m_obj.no_token_id] = {
                    "question": question,
                    "outcome": "No",
                    "event_slug": ev_slug,
                    "market_slug": market_slug,
                    "condition_id": m_obj.condition_id,
                }
            now_ts = datetime.now(UTC)
            thesis_position_value = 0.0     # sum(qty * avg_cost) — if positions revert to entry
            liquidation_position_value = 0.0  # sum(qty * last_mark) — current MTM
            for p in pipeline.ledger.positions():
                tm = compute_trade_metrics(
                    entry_price=float(p.avg_cost) if p.avg_cost else None,
                    size_units=abs(float(p.qty)),
                    edge=0.0,
                )
                end_date = tok_to_end.get(p.token_id)
                days_to_resolution: float | None = None
                if end_date is not None:
                    days_to_resolution = max(
                        0.0, (end_date - now_ts).total_seconds() / 86_400.0
                    )
                qty_f = float(p.qty)
                avg_cost_f = float(p.avg_cost)
                last_mark_f = float(p.last_mark)
                unrealized = qty_f * (last_mark_f - avg_cost_f)
                thesis_position_value += qty_f * avg_cost_f
                liquidation_position_value += qty_f * last_mark_f
                meta = tok_to_meta.get(p.token_id) or {}
                ev_slug = meta.get("event_slug")
                mkt_slug = meta.get("market_slug")
                # Polymarket routes by event slug — fall back to market slug,
                # then to a generic search-by-condition_id if neither exists.
                if ev_slug:
                    polymarket_url = f"https://polymarket.com/event/{ev_slug}"
                elif mkt_slug:
                    polymarket_url = f"https://polymarket.com/market/{mkt_slug}"
                else:
                    polymarket_url = None
                open_positions_out.append({
                    "token_id": p.token_id,
                    "qty": qty_f,
                    "avg_cost": avg_cost_f,
                    "last_mark": last_mark_f,
                    "unrealized_pnl": unrealized,
                    "entry": entry_by_token.get(p.token_id),
                    "trade_metrics": tm.asdict() if tm is not None else None,
                    "category": tok_to_cat.get(p.token_id),
                    "days_to_resolution": days_to_resolution,
                    "end_date_iso": end_date.isoformat() if end_date else None,
                    # Market metadata so the row is HUMAN-READABLE instead
                    # of "758459207417…". Operator can click ↗ to open
                    # the market on Polymarket directly.
                    "question": meta.get("question"),
                    "outcome": meta.get("outcome"),
                    "condition_id": meta.get("condition_id"),
                    "polymarket_url": polymarket_url,
                })
            # thesis NAV = cash + sum(qty * avg_cost). If every open position
            # eventually MTM'd back to the price we paid (which is where our
            # strategy thought fair value was), this is the expected NAV.
            # Liquidation NAV (snap.nav_usd) is what we'd get if we exited now.
            thesis_nav = float(pipeline.ledger.cash) + thesis_position_value
            broker.update_portfolio(
                nav_usd=snap.nav_usd,
                cash_usd=snap.cash_usd,
                open_position_count=snap.open_position_count,
                daily_pnl_pct=snap.daily_pnl_pct,
                total_exposure_pct=snap.total_exposure_pct,
                open_positions=open_positions_out,
                thesis_nav_usd=thesis_nav,
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
            # Slippage drift summary — populated as fills accumulate.
            sm = getattr(pipeline, "slippage_monitor", None)
            if sm is not None:
                try:
                    summary = sm.summary()
                    broker.snapshot.slippage_summary = summary
                    # Phase N.8: feed live drift to the risk policy so the
                    # auto-throttle (halve size when drift > 200 bps) is
                    # reactive instead of just observability.
                    try:
                        pipeline.risk.update_slippage_drift(summary.get("drift_bps"))
                    except Exception:
                        pass
                except Exception:
                    pass

            # Trade-flow funnel: strategy signals → aggregator → risk → orders.
            # Surfaces the drop-off so we can see WHY an order didn't fire
            # (aggregator conflict vs risk reject vs no signal at all).
            broker.update_pipeline_funnel(
                signals_emitted=_counter_total(PM_SIGNAL_EMITTED),
                signals_aggregated=_counter_total(PM_SIGNAL_AGGREGATED),
                risk_accepted=_counter_total_filtered(PM_RISK_DECISION, "decision", "accept"),
                risk_rejected=_counter_total_filtered(PM_RISK_DECISION, "decision", "reject"),
                orders_submitted=_counter_total(PM_ORDER_SUBMITTED),
            )
            # Push full snapshot so the UI's SSE subscriber refreshes ticks /
            # markets / NAV / counters every 5s without re-fetching REST.
            broker.emit_snapshot()
        except Exception as exc:
            # Was a silent `except: pass` — that mask is exactly how the
            # market_cache NameError bug stayed hidden across deploys.
            # Log the type + message so future regressions surface in
            # Railway logs immediately.
            log.warning(
                "broker_refresh.cycle_error",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
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
                # Gamma's /markets returns category=None — categories live in
                # /events as a `tags` array. Fetch events and derive a
                # canonical category per event, then propagate it onto each
                # market row via eventId lookup.
                try:
                    events_raw = await g.iter_active_events()
                    cat_map = build_event_category_map(events_raw)
                    attached = 0
                    for r in raw:
                        eid = extract_event_id(r)
                        if eid is None:
                            continue
                        derived = cat_map.get(eid)
                        if derived:
                            r["category"] = derived
                            attached += 1
                    log.info(
                        "gamma_sync.category_attached",
                        events=len(events_raw),
                        with_category=len(cat_map),
                        markets_with_category=attached,
                        markets_total=len(raw),
                    )
                except Exception as exc:
                    log.warning("gamma_sync.category_derive_failed", error=str(exc))
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
                    # Push a compact projection of every market into the
                    # broker so /api/markets can serve the full directory
                    # without re-hitting Gamma each request. Strip Gamma's
                    # huge raw blobs — the UI only needs the headline fields.
                    directory: list[dict[str, Any]] = []
                    for r in raw:
                        directory.append({
                            "condition_id": r.get("conditionId") or r.get("condition_id"),
                            "question": r.get("question"),
                            "category": r.get("category") or "Other",
                            "liquidity": float(r.get("liquidityNum") or r.get("liquidity") or 0),
                            "volume": float(r.get("volumeNum") or r.get("volume") or 0),
                            "end_date": r.get("endDateIso") or r.get("endDate"),
                            "active": bool(r.get("active", True)),
                            "closed": bool(r.get("closed", False)),
                        })
                    broker.update_markets_directory(directory)
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


async def _live_user_ws_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    log: Any,
) -> None:
    """Phase K.2 — CLOB user-channel WS plumbed into LiveExecutor.

    Live-mode only. Subscribes to wss://ws-subscriptions-clob.polymarket.com/ws/user
    with our HMAC-signed API credentials and routes every order_update /
    trade event into LiveExecutor.apply_user_ws_event(). That's the primary
    fill notification path — reconcile() polling is just the fallback for
    missed events.

    No-op when:
      - settings.mode is paper (LiveExecutor isn't even instantiated)
      - Polymarket API creds (key/secret/passphrase) are unset
    """
    settings = get_settings()
    if str(settings.mode) not in ("live-conservative", "live-normal"):
        log.info("user_ws.skipped", reason="paper_mode")
        return

    executor = getattr(pipeline, "executor", None)
    if executor is None or not hasattr(executor, "apply_user_ws_event"):
        log.warning("user_ws.skipped", reason="executor_missing_handler")
        return

    has_creds = bool(
        settings.polymarket_api_key.get_secret_value()
        and settings.polymarket_api_secret.get_secret_value()
        and settings.polymarket_passphrase.get_secret_value()
    )
    if not has_creds:
        log.warning("user_ws.skipped", reason="api_creds_missing")
        return

    src = ClobUserChannel()
    await src.start()
    log.info("user_ws.attached")
    try:
        async for evt in src.events():
            if stop.is_set():
                break
            try:
                await executor.apply_user_ws_event(evt)
            except Exception as exc:
                log.warning("user_ws.apply_failed", error=str(exc)[:120])
    finally:
        await src.stop()


async def _smart_money_feed_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    log: Any,
) -> None:
    """Polymarket data-api /trades poller → ClusterStateBuilder.

    Phase I.1: real trade feed for Smart Money. Polls every 5s, dedups by
    transactionHash, emits polymarket_trade events. ClusterStateBuilder
    aggregates per-condition flows; SmartMoneyStrategy reads the cluster
    state when a market evaluates.

    Tier classification: wallets are dynamically classified from their
    rolling stats (trade_count + total_volume_usd) — Tier 1 / 2 / 3
    thresholds applied here so the strategy's wallet_tier map updates
    live as new whales appear.
    """
    cb = getattr(pipeline, "cluster_builder", None)
    if cb is None:
        log.warning("smart_money_feed.no_cluster_builder")
        return

    # Phase O.1 — persist every public trade to the `trades` table so the
    # Replayer engine has real history to backtest against. Fire-and-forget;
    # DB outage warnings logged but never block the feed.
    def _persist_trade(evt: dict[str, Any]) -> None:
        async def _go() -> None:
            try:
                db = await get_db()
                ts_val = evt.get("ts")
                if not isinstance(ts_val, datetime):
                    return
                await insert_trade(
                    db,
                    ts=ts_val,
                    token_id=str(evt.get("asset") or ""),
                    side=str(evt.get("side") or "BUY"),
                    price=Decimal(str(evt.get("price") or 0)),
                    size=Decimal(str(evt.get("size_units") or 0)),
                    maker_address=None,
                    taker_address=str(evt.get("wallet") or "") or None,
                    tx_hash=str(evt.get("tx_hash") or "") or None,
                    is_ours=False,
                )
            except Exception as exc:
                log.debug("persist.trade_failed", error=str(exc)[:120])
        try:
            asyncio.create_task(_go())
        except RuntimeError:
            pass

    src = PolymarketTradesSource(poll_sec=5, on_trade_persist=_persist_trade)
    await src.start()

    # Spawn the cluster builder consumer task — drains src.events() into
    # per-condition flows automatically.
    await cb.start(src.events())

    # Tier classification thresholds — conservative defaults. Operator can
    # tune via env / config later. The /trades firehose only has volume +
    # count (no per-wallet P&L), so tiers are volume-based here.
    TIER1_MIN_VOLUME = 50_000.0
    TIER1_MIN_TRADES = 20
    TIER2_MIN_VOLUME = 10_000.0
    TIER2_MIN_TRADES = 5
    REFRESH_SEC = 30

    try:
        while not stop.is_set():
            try:
                stats = src.wallet_stats()
                strategy = pipeline.smart_money
                for wallet, s in stats.items():
                    vol = float(s.get("total_volume_usd", 0))
                    n = int(s.get("trade_count", 0))
                    if vol >= TIER1_MIN_VOLUME and n >= TIER1_MIN_TRADES:
                        tier = 1
                    elif vol >= TIER2_MIN_VOLUME and n >= TIER2_MIN_TRADES:
                        tier = 2
                    else:
                        tier = 3
                    cb.register_wallet_tier(wallet, tier)
                    if strategy is not None:
                        # SmartMoneyStrategy reads wallet_tier from its own
                        # map — push there too.
                        try:
                            strategy._wallet_tier[wallet] = tier  # type: ignore[reportPrivateUsage]
                        except Exception:
                            pass
                if stats:
                    n1 = sum(
                        1 for s in stats.values()
                        if s.get("total_volume_usd", 0) >= TIER1_MIN_VOLUME
                        and s.get("trade_count", 0) >= TIER1_MIN_TRADES
                    )
                    n2 = sum(
                        1 for s in stats.values()
                        if (
                            TIER2_MIN_VOLUME <= s.get("total_volume_usd", 0) < TIER1_MIN_VOLUME
                            or s.get("trade_count", 0) < TIER1_MIN_TRADES
                        )
                        and s.get("total_volume_usd", 0) >= TIER2_MIN_VOLUME
                    )
                    log.debug(
                        "smart_money_feed.tiers",
                        total=len(stats),
                        tier1=n1,
                        tier2=n2,
                    )
            except Exception as exc:
                log.warning("smart_money_feed.refresh_error", error=str(exc))

            try:
                await asyncio.wait_for(stop.wait(), timeout=REFRESH_SEC)
                return
            except asyncio.TimeoutError:
                continue
    finally:
        await cb.stop()
        await src.stop()


async def _portfolio_persist_loop(
    stop: asyncio.Event,
    pipeline: Pipeline,
    log: Any,
) -> None:
    """Phase H.5 — mirror positions to DB every 30s + write pnl_daily once
    per day. Unblocks the promotion gate (paper-track needs ≥7 daily rows)
    and means a Railway restart no longer wipes the position book.

    Each cycle is fire-and-forget — DB outage warnings logged but never
    bubble up to crash the trading loop.
    """
    INTERVAL_SEC = 30
    last_pnl_date: Any = None
    while not stop.is_set():
        try:
            db = await get_db()
            # Mirror current positions. Closed positions (qty=0) get deleted.
            for p in pipeline.ledger.positions():
                try:
                    await upsert_position(
                        db,
                        token_id=p.token_id,
                        qty=p.qty,
                        avg_cost=p.avg_cost,
                        last_mark=p.last_mark,
                        last_updated=p.last_updated,
                    )
                except Exception as exc:
                    log.debug("persist.position_failed", error=str(exc)[:120])

            # Daily P&L summary — write once per UTC day. Idempotent ON CONFLICT
            # so the within-day refreshes update the same row. Uses N.3/N.6-corrected
            # daily_roll_up (real win_count by avg_cost comparison + fees-aware
            # realized PnL on each position via Ledger.apply_fill).
            from poly_meridian.portfolio.pnl import daily_roll_up
            now = datetime.now(UTC)
            today = now.date()
            # Phase N.5: rotate start-of-day NAV at UTC midnight. The pipeline
            # carries a `day_start_nav` attr; we snapshot it on first persist
            # of a new day and pass to snapshot() so daily_pnl_pct is correct.
            current_nav = Decimal(str(nav_usd_helper(pipeline.ledger)))
            if last_pnl_date != today:
                pipeline.day_start_nav = current_nav  # type: ignore[attr-defined]
            day_start = getattr(pipeline, "day_start_nav", current_nav)
            snap = snapshot(pipeline.ledger, day_start_nav=day_start)
            roll = daily_roll_up(pipeline.ledger, today)
            try:
                await upsert_pnl_daily(
                    db,
                    date=now,
                    starting_nav=Decimal(str(roll["starting_nav"])),
                    ending_nav=Decimal(str(roll["ending_nav"])),
                    realized=Decimal(str(roll["realized"])),
                    unrealized=Decimal(str(roll["unrealized"])),
                    fees=Decimal(str(roll["fees"])),
                    trade_count=int(roll["trade_count"]),
                    win_count=int(roll["win_count"]),
                )
                if last_pnl_date != today:
                    log.info("persist.pnl_daily_started", date=str(today),
                             day_start_nav=str(day_start))
                    last_pnl_date = today
            except Exception as exc:
                log.debug("persist.pnl_daily_failed", error=str(exc)[:120])
        except Exception as exc:
            log.warning("portfolio_persist.cycle_error", error=str(exc)[:120])

        try:
            await asyncio.wait_for(stop.wait(), timeout=INTERVAL_SEC)
            return
        except asyncio.TimeoutError:
            continue


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
                    pipeline.stat_quant, pipeline.fundamentals,
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
    fund_cfg = _load_yaml("strategies/fundamentals.yaml")
    limits = _load_risk_limits()
    settings = get_settings()

    arbitrage = ArbitrageStrategy(arb_cfg)
    sentiment = SentimentStrategy(sent_cfg)
    smart_money = SmartMoneyStrategy(sm_cfg)
    stat_quant = StatQuantStrategy(sq_cfg)
    fundamentals = FundamentalsStrategy(fund_cfg)

    aggregator = SignalAggregator(max_size_pct_per_position=limits.max_position_pct_of_bankroll)
    starting_nav = Decimal("100000") if str(settings.mode) == "paper" else Decimal("500")
    ledger = Ledger(starting_cash_usd=starting_nav)
    executor = _build_executor(str(settings.mode))
    # Slippage drift monitor — every fill feeds an observation; main.py runs
    # a sampler loop that re-fits and alerts on drift. PaperExecutor and
    # LiveExecutor both honor attach_slippage_monitor() since the contract
    # is observation-only (no behavior change in execution itself).
    slippage_monitor = SlippageMonitor()
    if hasattr(executor, "attach_slippage_monitor"):
        executor.attach_slippage_monitor(slippage_monitor)
    risk = DefaultRiskPolicy(strategy_name="poly_meridian", limits=limits)

    pipeline = Pipeline(
        arbitrage=arbitrage,
        sentiment=sentiment,
        smart_money=smart_money,
        stat_quant=stat_quant,
        fundamentals=fundamentals,
        aggregator=aggregator,
        risk=risk,
        executor=executor,
        ledger=ledger,
    )
    executor._on_fill = pipeline.on_fill  # type: ignore[attr-defined]

    # Expose the slippage monitor on the pipeline so the periodic
    # alert loop can read fit + drift without smuggling state.
    pipeline.slippage_monitor = slippage_monitor  # type: ignore[attr-defined]

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
    from poly_meridian import __version__
    log.info("agent.boot", mode=settings.mode, version=__version__)

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
            pipeline.stat_quant, pipeline.fundamentals,
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
            # FLATTEN: cancel every open order so engaging the switch
            # actually closes risk, not just blocks new orders. Fire-and-
            # forget so the broker hook returns immediately. PaperExecutor
            # + LiveExecutor both implement cancel_all_open_orders.
            executor = getattr(pipeline, "executor", None)
            if executor is not None and hasattr(executor, "cancel_all_open_orders"):
                async def _flatten() -> None:
                    try:
                        n = await executor.cancel_all_open_orders()
                        log.warning("kill_switch.flatten", cancelled=n)
                        if n > 0:
                            await slack_alert_async(
                                f"kill-switch flatten: cancelled {n} open orders",
                                level="warn",
                            )
                    except Exception as exc:
                        log.warning("kill_switch.flatten_failed", error=str(exc)[:120])
                try:
                    asyncio.create_task(_flatten())
                except RuntimeError:
                    pass
        else:
            post_slack_alert("kill-switch disengaged · trading resumed", level="warn")

    broker.set_first_signal_hook(_alert_first_signal)
    broker.set_first_order_hook(_alert_first_order)
    broker.set_kill_switch_hook(_alert_kill_switch)

    # ---- DB persistence hooks (Phase G) ----
    # Mirror every signal + order to Postgres so the dashboard survives
    # Railway restarts. Errors are swallowed inside push_* so a DB outage
    # never blocks the trading loop.
    def _persist_signal(sig: dict[str, Any]) -> None:
        if not db_ok:
            return
        try:
            ts_str = sig.get("ts")
            ts = datetime.fromisoformat(ts_str) if isinstance(ts_str, str) else None
        except Exception:
            ts = None
        if ts is None:
            from datetime import datetime as _dt
            from datetime import UTC as _UTC
            ts = _dt.now(_UTC)

        async def _go() -> None:
            try:
                db = await get_db()
                await insert_strategy_signal(
                    db,
                    ts=ts,
                    strategy=str(sig.get("strategy") or "?"),
                    condition_id=str(sig.get("condition_id") or ""),
                    token_id=str(sig.get("token_id") or ""),
                    edge=float(sig.get("edge") or 0.0),
                    conviction=float(sig.get("conviction") or 0.0),
                    suggested_action=str(sig.get("suggested_action") or "HOLD"),
                    rationale=sig.get("rationale") or {},
                )
            except Exception as exc:
                log.debug("persist.signal_failed", error=str(exc)[:120])
        try:
            asyncio.create_task(_go())
        except RuntimeError:
            pass

    def _persist_order(order: dict[str, Any]) -> None:
        if not db_ok:
            return
        try:
            ts_str = order.get("ts")
            ts_created = datetime.fromisoformat(ts_str) if isinstance(ts_str, str) else None
        except Exception:
            ts_created = None
        if ts_created is None:
            from datetime import datetime as _dt
            from datetime import UTC as _UTC
            ts_created = _dt.now(_UTC)

        async def _go() -> None:
            try:
                db = await get_db()
                # The schema CHECKs reject lowercased side/status; uppercase
                # them defensively. mode stays lowercase ("paper").
                side = str(order.get("side") or "BUY").upper()
                status = str(order.get("status") or "PENDING").upper()
                await upsert_order(
                    db,
                    order_id=str(order.get("order_id") or ""),
                    ts_created=ts_created,
                    ts_filled=None,
                    strategy=str(order.get("strategy") or "?"),
                    token_id=str(order.get("token_id") or ""),
                    side=side,
                    order_type="GTC",
                    price=Decimal(str(order["price"])) if order.get("price") is not None else None,
                    size=Decimal(str(order.get("size") or 0)),
                    filled_size=Decimal(str(order.get("filled_size") or 0)),
                    avg_fill_price=(
                        Decimal(str(order["avg_fill_price"]))
                        if order.get("avg_fill_price") is not None else None
                    ),
                    status=status,
                    mode=str(order.get("mode") or "paper"),
                )
            except Exception as exc:
                log.debug("persist.order_failed", error=str(exc)[:120])
        try:
            asyncio.create_task(_go())
        except RuntimeError:
            pass

    broker.set_persistence_hooks(
        signal_hook=_persist_signal,
        order_hook=_persist_order,
    )

    # Phase L.2 — every Ledger fill mirrored to DB ledger_entries. Pipeline
    # captures the fill, hands a dict to this hook which fires async insert.
    def _persist_ledger_entry(payload: dict[str, Any]) -> None:
        if not db_ok:
            return
        async def _go() -> None:
            try:
                db = await get_db()
                await insert_ledger_entry(
                    db,
                    ts=payload["ts"],
                    order_id=str(payload["order_id"]),
                    fill_seq=int(payload["fill_seq"]),
                    strategy=str(payload["strategy"]),
                    token_id=str(payload["token_id"]),
                    side=str(payload["side"]),
                    qty=Decimal(str(payload["qty"])),
                    price=Decimal(str(payload["price"])),
                    notional=Decimal(str(payload["notional"])),
                    fee=Decimal(str(payload["fee"])),
                    realized_pnl=(
                        Decimal(str(payload["realized_pnl"]))
                        if payload.get("realized_pnl") is not None else None
                    ),
                )
            except Exception as exc:
                log.debug("persist.ledger_entry_failed", error=str(exc)[:120])
        try:
            asyncio.create_task(_go())
        except RuntimeError:
            pass
    pipeline.on_ledger_entry = _persist_ledger_entry  # type: ignore[attr-defined]

    # ---- Boot backfill: restore last 50 signals/orders from DB so the
    # dashboard doesn't go blank after a Railway restart. -----------
    if db_ok:
        try:
            _db = await get_db()
            past_orders = await fetch_recent_orders(_db, limit=50)
            past_signals = await fetch_recent_strategy_signals(_db, limit=50)

            def _normalize_signal(r: dict[str, Any]) -> dict[str, Any]:
                rat = r.get("rationale")
                if isinstance(rat, str):
                    try:
                        rat = json.loads(rat)
                    except Exception:
                        rat = {}
                return {
                    "ts": r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"],
                    "strategy": r.get("strategy"),
                    "condition_id": r.get("condition_id"),
                    "token_id": r.get("token_id"),
                    "edge": float(r.get("edge") or 0),
                    "conviction": float(r.get("conviction") or 0),
                    "suggested_action": r.get("suggested_action"),
                    "rationale": rat or {},
                }

            def _normalize_order(r: dict[str, Any]) -> dict[str, Any]:
                return {
                    "ts": (r["ts_created"].isoformat() if hasattr(r["ts_created"], "isoformat") else r["ts_created"]),
                    "order_id": r.get("order_id"),
                    "strategy": r.get("strategy"),
                    "token_id": r.get("token_id"),
                    "side": r.get("side"),
                    "status": r.get("status"),
                    "price": r.get("price"),
                    "size": r.get("size"),
                    "filled_size": r.get("filled_size"),
                    "avg_fill_price": r.get("avg_fill_price"),
                    "mode": r.get("mode"),
                }

            broker.seed_signals([_normalize_signal(r) for r in past_signals])
            broker.seed_orders([_normalize_order(r) for r in past_orders])
            log.info(
                "broker.backfilled",
                signals=len(past_signals),
                orders=len(past_orders),
            )
        except Exception as exc:
            log.warning("broker.backfill_failed", error=str(exc))

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
            _broker_refresh_loop(
                stop_event, pipeline, broker, news_proc, market_cache,
            ),
            name="broker_refresh",
        ),
    ]
    if news_proc is not None and db_ok:
        tasks.append(
            asyncio.create_task(
                _news_process_loop(stop_event, news_proc, log), name="news_process"
            )
        )
    if db_ok:
        # Phase H.5 — mirror positions + pnl_daily so promotion gate has data.
        tasks.append(
            asyncio.create_task(
                _portfolio_persist_loop(stop_event, pipeline, log),
                name="portfolio_persist",
            )
        )
    # Phase I.1 — Smart Money real feed via Polymarket data-api /trades.
    # Runs independent of DB (events drive cluster_builder in-memory).
    tasks.append(
        asyncio.create_task(
            _smart_money_feed_loop(stop_event, pipeline, log),
            name="smart_money_feed",
        )
    )

    # Phase N.3 — ExitMonitor. Scans positions every 10s, emits SELL on
    # profit-take (+20%), stop-loss (-30%), or time-decay (<6h to resolution).
    # Routes via executor directly so kill-switch flatten (L.1) still applies.
    exit_monitor = ExitMonitor(
        ledger=pipeline.ledger,
        executor=pipeline.executor,
        broker=broker,
        market_cache=market_cache,
        kill_switch=pipeline.risk.kill_switch,
    )
    pipeline.exit_monitor = exit_monitor  # type: ignore[attr-defined]
    tasks.append(
        asyncio.create_task(
            exit_monitor.run_loop(stop_event), name="exit_monitor",
        )
    )
    # Phase K.2 — CLOB user-channel WS feeds LiveExecutor fill notifications.
    # No-op in paper mode (returns immediately). In live mode without creds
    # it also no-ops with a clear log.
    tasks.append(
        asyncio.create_task(
            _live_user_ws_loop(stop_event, pipeline, log),
            name="live_user_ws",
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
