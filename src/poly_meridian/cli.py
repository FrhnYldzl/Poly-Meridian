"""CLI — typer entrypoints for run, backtest, walkforward, promote-to-live. §10."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import typer

from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.settings import get_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def run() -> None:
    """Start the agent (respects MODE env var; default = paper)."""
    from poly_meridian.main import main as _main

    _main()


@app.command()
def status() -> None:
    """Print the current effective settings and mode."""
    s = get_settings()
    typer.echo(f"mode:          {s.mode}")
    typer.echo(f"postgres_url:  {s.postgres_url}")
    typer.echo(f"redis_url:     {s.redis_url}")
    typer.echo(f"clob_host:     {s.polymarket_clob_host}")


@app.command()
def backtest(
    strategies: list[str] = typer.Option(["arbitrage"], "--strategy", "-s"),
    days: int = typer.Option(90, "--days", "-d"),
    out_dir: Path = typer.Option(Path("reports"), "--out"),
    starting_nav: float = typer.Option(100_000, "--nav"),
    tick_interval_sec: int = typer.Option(60, "--tick-sec"),
) -> None:
    """Run a backtest from DB history."""
    import yaml

    from poly_meridian.backtest import (
        ReplayConfig,
        Replayer,
        compute_all,
        write_report,
    )
    from poly_meridian.risk import DefaultRiskPolicy, RiskLimits
    from poly_meridian.strategies import (
        ArbitrageStrategy,
        SentimentStrategy,
        SignalAggregator,
        SmartMoneyStrategy,
        StatQuantStrategy,
    )

    configure_logging(get_settings().log_level)

    def _load_strategy(name: str) -> object:
        cfg_path = get_settings().config_dir / "strategies" / f"{name}.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        cfg = cfg or {}
        cfg["enabled"] = True
        if name == "arbitrage":
            return ArbitrageStrategy(cfg)
        if name == "sentiment":
            return SentimentStrategy(cfg)
        if name == "smart_money":
            return SmartMoneyStrategy(cfg)
        if name == "stat_quant":
            return StatQuantStrategy(cfg)
        if name == "fundamentals":
            from poly_meridian.strategies import FundamentalsStrategy
            return FundamentalsStrategy(cfg)
        raise typer.BadParameter(f"unknown strategy: {name}")

    def _load_risk_limits() -> RiskLimits:
        cfg = get_settings().config_dir / "risk.yaml"
        if not cfg.exists():
            return RiskLimits()
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        r = (data or {}).get("risk", {})
        return RiskLimits(
            kelly_fraction=float(r.get("kelly_fraction", 0.25)),
            max_position_pct_of_bankroll=float(r.get("max_position_pct_of_bankroll", 0.05)),
            max_exposure_per_category_pct=float(r.get("max_exposure_per_category_pct", 0.30)),
            max_total_exposure_pct=float(r.get("max_total_exposure_pct", 0.80)),
            daily_max_loss_pct=float(r.get("daily_max_loss_pct", 0.05)),
            weekly_max_loss_pct=float(r.get("weekly_max_loss_pct", 0.10)),
            max_concentration_single_event_pct=float(r.get("max_concentration_single_event_pct", 0.10)),
            max_open_positions=int(r.get("max_open_positions", 50)),
            min_market_liquidity_usd=float(r.get("min_market_liquidity_usd", 10_000)),
            max_position_pct_of_market_volume=float(r.get("max_position_pct_of_market_volume", 0.05)),
        )

    async def _go() -> None:
        from poly_meridian.backtest.loader import load_dataset_from_db
        from poly_meridian.storage import close_db, get_db

        end_ts = datetime.now(UTC)
        start_ts = end_ts - timedelta(days=days)

        db = await get_db()
        try:
            dataset = await load_dataset_from_db(db, start_ts=start_ts, end_ts=end_ts)
        finally:
            await close_db()

        strats = [_load_strategy(n) for n in strategies]  # type: ignore[arg-type]
        limits = _load_risk_limits()
        aggregator = SignalAggregator(max_size_pct_per_position=limits.max_position_pct_of_bankroll)
        risk = DefaultRiskPolicy(strategy_name="backtest", limits=limits)
        replayer = Replayer(
            dataset=dataset,
            strategies=strats,            # type: ignore[arg-type]
            aggregator=aggregator,
            risk=risk,
            config=ReplayConfig(
                starting_nav_usd=Decimal(str(starting_nav)),
                tick_interval_sec=tick_interval_sec,
            ),
        )
        result = await replayer.run()

        duration_sec = result.duration_sec or 1.0
        metrics = compute_all(
            equity_curve=result.equity_curve,
            trade_pnls=result.trade_pnls,
            duration_sec=duration_sec,
            period_sec=tick_interval_sec,
        )
        md_path, json_path = write_report(
            out_dir=out_dir,
            title=f"Backtest: {', '.join(strategies)}",
            metrics=metrics,
            result=result,
            strategy_names=strategies,
            slug="-".join(strategies) + "-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
            extra={"n_ticks": str(len(dataset.ticks))},
        )
        typer.echo(f"Report: {md_path}")
        typer.echo(f"JSON:   {json_path}")

    asyncio.run(_go())


@app.command()
def walkforward(
    strategies: list[str] = typer.Option(["arbitrage"], "--strategy", "-s"),
    total_days: int = typer.Option(180, "--total-days"),
    train_days: int = typer.Option(60, "--train-days"),
    test_days: int = typer.Option(15, "--test-days"),
) -> None:
    """Walk-forward summary: list folds."""
    from poly_meridian.backtest import make_folds

    end = datetime.now(UTC)
    start = end - timedelta(days=total_days)
    folds = make_folds(start=start, end=end, train_days=train_days, test_days=test_days)
    typer.echo(f"strategies: {','.join(strategies)}")
    typer.echo(f"folds: {len(folds)}")
    for f in folds:
        typer.echo(
            f"  fold {f.fold_index}: train {f.train_start.date()}→{f.train_end.date()} "
            f"test {f.test_start.date()}→{f.test_end.date()}"
        )


@app.command("promote-to-live")
def promote_to_live_cmd(
    proposed_live_usd: float = typer.Option(500.0),
    min_paper_days: int = typer.Option(30),
    min_sharpe: float = typer.Option(1.2),
    max_drawdown: float = typer.Option(0.20),
) -> None:
    """Run the §19 promotion gate. Exit 0 on PASS, 1 on FAIL."""
    from poly_meridian.promotion import run_gate
    from poly_meridian.storage import close_db, get_db

    configure_logging(get_settings().log_level)

    async def _go() -> int:
        db = await get_db()
        try:
            report = await run_gate(
                db,
                proposed_live_usd=Decimal(str(proposed_live_usd)),
                min_paper_days=min_paper_days,
                min_sharpe=min_sharpe,
                max_drawdown=max_drawdown,
            )
        finally:
            await close_db()
        typer.echo(report.render())
        return 0 if report.passed else 1

    raise typer.Exit(code=asyncio.run(_go()))


@app.command("mark-drill")
def mark_drill_cmd(
    name: str = typer.Argument(..., help="kill_switch | reconnect | secrets | backup | legal"),
) -> None:
    """Mark a §19 drill as completed (writes a flag under .promotion_flags/)."""
    from poly_meridian.promotion import mark_drill

    flag = mark_drill(name)
    typer.echo(f"Marked drill complete: {flag}")


if __name__ == "__main__":
    app()
