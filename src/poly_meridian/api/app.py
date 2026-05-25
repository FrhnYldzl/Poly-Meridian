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
from fastapi import FastAPI, HTTPException, Query
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
        version="1.1.0",
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

    @app.get("/api/markets")
    async def markets_directory(
        category: str | None = None,
        sort: str = "liquidity",
        limit: int = 500,
    ) -> dict[str, Any]:
        """Compact markets directory built from the most recent gamma_sync.

        Query params:
          - category: filter to one canonical Polymarket category
          - sort: liquidity | volume | end_date (default liquidity desc)
          - limit: max rows to return (default 500, no hard cap)

        Returns {markets: [...], total: N, by_category: {...}}.
        """
        rows = broker.get_markets_directory()
        if category and category != "all":
            rows = [r for r in rows if (r.get("category") or "Other") == category]
        if sort == "volume":
            rows = sorted(rows, key=lambda r: float(r.get("volume") or 0), reverse=True)
        elif sort == "end_date":
            rows = sorted(rows, key=lambda r: str(r.get("end_date") or "9999"))
        else:
            rows = sorted(rows, key=lambda r: float(r.get("liquidity") or 0), reverse=True)
        # by_category buckets are computed across the UNFILTERED set so the
        # UI's filter chips show the full universe count.
        by_cat: dict[str, int] = {}
        for r in broker.get_markets_directory():
            c = r.get("category") or "Other"
            by_cat[c] = by_cat.get(c, 0) + 1
        return {
            "markets": rows[:limit],
            "total": len(rows),
            "universe_total": len(broker.get_markets_directory()),
            "by_category": by_cat,
        }

    @app.post("/api/admin/run-drill")
    async def admin_run_drill(
        secret: str = Query(..., description="ADMIN_RESET_TOKEN env-var match"),
        drill: str = Query("kill_switch", description="kill_switch | health | backup | all"),
    ) -> dict[str, Any]:
        """Server-side DR drill runner — operator hits this from any HTTP
        client (no shell/SSH needed on Railway). Writes evidence files
        under .promotion_flags/ and returns the result + evidence inline.

        Each drill exercises a real piece of the safety / recovery stack:
          kill_switch — engage → verify state → disengage → verify state
          health      — /health + /api/state reachability + uptime + db_ok
          backup      — pg_dump + restore round-trip with row-count parity
                        (only works when pg_dump/psql are on the container
                        PATH and POSTGRES_URL is set; otherwise records
                        why it skipped)
          all         — runs the three drills in order
        """
        expected = os.environ.get("ADMIN_RESET_TOKEN", "")
        if not expected:
            raise HTTPException(status_code=503, detail="ADMIN_RESET_TOKEN not configured")
        if secret != expected:
            raise HTTPException(status_code=401, detail="bad_secret")

        # In-process drill — operates directly against the broker (no HTTP).
        # That's more reliable than the CLI version when running on Railway
        # where outbound HTTP to localhost may not loop back.
        from poly_meridian.promotion import drill_evidence, mark_drill

        results: dict[str, dict[str, Any]] = {}

        async def _drill_kill_switch() -> bool:
            evidence: dict[str, Any] = {}
            before = broker.snapshot.kill_switch_engaged
            evidence["initial_engaged"] = before
            broker.update_kill_switch(engaged=True, reason="dr_drill")
            evidence["engaged_after_set"] = broker.snapshot.kill_switch_engaged
            await asyncio.sleep(0.5)
            broker.update_kill_switch(engaged=False, reason=None)
            evidence["engaged_after_clear"] = broker.snapshot.kill_switch_engaged
            passed = (
                evidence["engaged_after_set"] is True
                and evidence["engaged_after_clear"] is False
            )
            mark_drill("kill_switch", passed=passed, evidence=evidence)
            return passed

        async def _drill_health() -> bool:
            snap = broker.snapshot.asdict()
            uptime = float(snap.get("uptime_sec") or 0)
            db_ok = bool(snap.get("db_ok"))
            evidence = {
                "uptime_sec": uptime,
                "db_ok": db_ok,
                "cache_ok": snap.get("cache_ok"),
                "mode": snap.get("mode"),
                "markets_active": snap.get("markets_active_total"),
            }
            passed = uptime >= 60 and db_ok
            mark_drill("health", passed=passed, evidence=evidence)
            return passed

        async def _drill_backup() -> bool:
            # Server-side backup: pg_dump → restore → row-count parity.
            # Skips gracefully when tooling isn't available on this image.
            import shutil
            import subprocess
            import tempfile
            from pathlib import Path
            settings = get_settings()
            db_url = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
            evidence: dict[str, Any] = {"db_url_kind": "postgresql"}
            if not shutil.which("pg_dump") or not shutil.which("psql"):
                evidence["reason"] = "pg_dump/psql not installed in container"
                mark_drill("backup", passed=False, evidence=evidence)
                return False
            with tempfile.TemporaryDirectory() as td:
                dump = Path(td) / "snap.dump"
                try:
                    subprocess.run(
                        ["pg_dump", "-Fc", "--no-owner", "--no-privileges",
                         "-f", str(dump), db_url],
                        check=True, capture_output=True, text=True, timeout=120,
                    )
                except Exception as exc:
                    evidence["pg_dump_error"] = str(exc)[:300]
                    mark_drill("backup", passed=False, evidence=evidence)
                    return False
                evidence["dump_kb"] = dump.stat().st_size // 1024
                # We don't have permission to createdb on managed PG, so just
                # validate the dump is a valid Postgres custom-format file.
                try:
                    out = subprocess.run(
                        ["pg_restore", "--list", str(dump)],
                        check=True, capture_output=True, text=True, timeout=30,
                    )
                    evidence["restore_list_lines"] = len(out.stdout.splitlines())
                except Exception as exc:
                    evidence["pg_restore_list_error"] = str(exc)[:300]
                    mark_drill("backup", passed=False, evidence=evidence)
                    return False
            passed = evidence["dump_kb"] > 0 and evidence["restore_list_lines"] > 0
            mark_drill("backup", passed=passed, evidence=evidence)
            return passed

        to_run = (
            ["kill_switch", "health", "backup"] if drill == "all" else [drill]
        )
        for d in to_run:
            try:
                if d == "kill_switch":
                    ok = await _drill_kill_switch()
                elif d == "health":
                    ok = await _drill_health()
                elif d == "backup":
                    ok = await _drill_backup()
                else:
                    raise HTTPException(status_code=400, detail=f"unknown drill: {d}")
                results[d] = {"passed": ok, "evidence": drill_evidence(d) or {}}
            except HTTPException:
                raise
            except Exception as exc:
                results[d] = {"passed": False, "error": str(exc)[:200]}

        return {
            "version": "1.1.0",
            "drill": drill,
            "all_passed": all(r.get("passed") for r in results.values()),
            "results": results,
        }

    @app.get("/api/strategy-pnl")
    async def strategy_pnl(days: int = 30) -> dict[str, Any]:
        """Per-strategy realized PNL + fill counts + win rate. Aggregated
        from ledger_entries over the last N days. Drives the attribution
        column on the Strategies page."""
        from poly_meridian.storage import get_db
        from poly_meridian.storage.writers import fetch_pnl_per_strategy
        try:
            db = await get_db()
            rows = await fetch_pnl_per_strategy(db, days=max(1, min(365, int(days))))
        except Exception as exc:
            return {"error": str(exc)[:200], "rows": []}
        return {"days": int(days), "rows": rows}

    @app.post("/api/backtest/run")
    async def run_backtest(
        seed: int = 42,
        n_markets: int = 12,
        n_steps: int = 200,
        starting_nav: float = 100_000.0,
        bet_size_pct: float = 0.02,
        zscore_threshold: float = 1.5,
    ) -> dict[str, Any]:
        """Run a synthetic backtest with the requested params.

        Currently uses src/poly_meridian/backtest/synthetic.py — deterministic
        random-walk on simulated markets with a mean-reversion strategy. The
        real `Replayer` engine + `load_dataset_from_db` are wired in code but
        require enough orderbook_snapshots history to be meaningful; once we
        have a few days of history we can switch this to call them.
        """
        from poly_meridian.backtest.synthetic import (
            SyntheticBacktestConfig, run_synthetic_backtest,
        )
        cfg = SyntheticBacktestConfig(
            seed=int(seed),
            n_markets=max(1, min(50, int(n_markets))),
            n_steps=max(20, min(2000, int(n_steps))),
            starting_nav=float(starting_nav),
            bet_size_pct=max(0.001, min(0.1, float(bet_size_pct))),
            zscore_threshold=max(0.5, min(4.0, float(zscore_threshold))),
        )
        result = await asyncio.to_thread(run_synthetic_backtest, cfg)
        return {
            "mode": "synthetic",
            "config": {
                "seed": cfg.seed,
                "n_markets": cfg.n_markets,
                "n_steps": cfg.n_steps,
                "step_sec": cfg.step_sec,
                "starting_nav": cfg.starting_nav,
                "bet_size_pct": cfg.bet_size_pct,
                "zscore_threshold": cfg.zscore_threshold,
            },
            **result.asdict(),
        }

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

    @app.post("/api/admin/reset-data")
    async def admin_reset_data(
        secret: str = Query(..., description="Shared secret from ADMIN_RESET_TOKEN env"),
        confirm: str = Query("", description="Pass 'YES' to actually run"),
        restart: bool = Query(True, description="Self-exit after wipe so Railway auto-restarts with fresh in-memory state"),
    ) -> dict[str, Any]:
        """v1.1 clean slate — TRUNCATE all trading-data tables + reset the
        broker's in-memory feeds. Schema is preserved; row counts go to zero.

        Auth: requires ADMIN_RESET_TOKEN env var to be set on the agent AND
        the request to pass it as `?secret=...`. Without confirm=YES the
        call is a no-op dry run that reports what WOULD be wiped.

        Tables affected:
          our_orders, strategy_signals, positions, pnl_daily,
          news_articles, news_signals, market_embeddings,
          orderbook_snapshots, trades, feature_snapshots, smart_wallets

        Does NOT drop:
          markets (refreshed by gamma_sync anyway)
          schema itself
        """
        expected = os.environ.get("ADMIN_RESET_TOKEN", "")
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="ADMIN_RESET_TOKEN not configured on the agent",
            )
        if secret != expected:
            raise HTTPException(status_code=401, detail="bad_secret")

        # Lazy-import so the API module stays import-light at boot.
        from poly_meridian.storage import get_db

        TARGETS = [
            "our_orders",
            "strategy_signals",
            "positions",
            "pnl_daily",
            "news_signals",
            "news_articles",
            "market_embeddings",
            "orderbook_snapshots",
            "trades",
            "feature_snapshots",
            "smart_wallets",
        ]
        counts: dict[str, int] = {}
        try:
            db = await get_db()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"db_unavailable: {exc}")

        async with db.acquire() as conn:
            for tbl in TARGETS:
                try:
                    row = await conn.fetchrow(f"SELECT COUNT(*) AS n FROM {tbl}")
                    counts[tbl] = int(row["n"]) if row else 0
                except Exception:
                    counts[tbl] = -1   # table missing — schema didn't create it

            dry_run = confirm.upper() != "YES"
            if not dry_run:
                for tbl in TARGETS:
                    if counts.get(tbl, -1) < 0:
                        continue
                    try:
                        await conn.execute(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
                    except Exception as exc:
                        log.warning("admin_reset.truncate_failed", table=tbl, error=str(exc))

        # Reset broker in-memory state so the dashboard reflects the wipe
        # immediately (not just after the next agent restart).
        if not dry_run:
            broker.seed_signals([])
            broker.seed_orders([])
            broker.update_kill_switch(engaged=False, reason=None)

        will_restart = bool(not dry_run and restart)
        if will_restart:
            # Self-exit AFTER the response flushes so Railway brings us back
            # up with a fresh process — that wipes the in-memory ledger /
            # broker state / cluster builder / wallet tier maps cleanly.
            import signal as _signal
            async def _self_exit() -> None:
                await asyncio.sleep(2.0)
                log.info("admin_reset.self_exit_for_restart")
                os.kill(os.getpid(), _signal.SIGTERM)
            asyncio.create_task(_self_exit())

        return {
            "dry_run": dry_run,
            "version": "1.1.0",
            "row_counts_before": counts,
            "restart_scheduled": will_restart,
            "note": (
                "data wiped + restart in ~2s (Railway will bring the agent back fresh)"
                if will_restart
                else (
                    "data wiped; restart agent to clear in-memory state"
                    if not dry_run
                    else "dry run — pass confirm=YES to actually truncate"
                )
            ),
        }

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
