"""Backtest reports — markdown + JSON. See MASTER_SPEC §18."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from poly_meridian.backtest.metrics import PerformanceMetrics, meets_live_gate
from poly_meridian.backtest.replay import ReplayResult


def report_markdown(
    *,
    title: str,
    metrics: PerformanceMetrics,
    result: ReplayResult,
    strategy_names: list[str],
    extra: dict[str, Any] | None = None,
) -> str:
    passes, failures = meets_live_gate(metrics)
    status = "✅ PASS" if passes else f"⚠️ FAIL ({len(failures)} failures)"

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**Strategies:** {', '.join(strategy_names)}")
    if result.start_ts and result.end_ts:
        lines.append(f"**Period:** {result.start_ts.isoformat()} → {result.end_ts.isoformat()}")
    lines.append(f"**Live-gate (§18):** {status}")
    if failures:
        lines.append("")
        for f in failures:
            lines.append(f"- ❌ {f}")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total return | {metrics.total_return:.2%} |")
    lines.append(f"| CAGR | {metrics.cagr:.2%} |")
    lines.append(f"| Annual volatility | {metrics.volatility_annual:.2%} |")
    lines.append(f"| Sharpe | {metrics.sharpe:.2f} |")
    lines.append(f"| Sortino | {metrics.sortino:.2f} |")
    lines.append(f"| Calmar | {metrics.calmar:.2f} |")
    lines.append(f"| Max drawdown | {metrics.max_drawdown:.2%} |")
    lines.append(f"| Win rate | {metrics.win_rate:.2%} |")
    lines.append(f"| Profit factor | {metrics.profit_factor:.2f} |")
    lines.append(f"| Expectancy / trade | ${metrics.expectancy_usd:.2f} |")
    lines.append(f"| Trade count | {metrics.trade_count} |")
    lines.append(f"| Final NAV | ${result.final_nav:,.2f} |")

    if extra:
        lines.append("")
        lines.append("## Notes")
        for k, v in extra.items():
            lines.append(f"- **{k}:** {v}")

    return "\n".join(lines) + "\n"


def report_json(
    *,
    metrics: PerformanceMetrics,
    result: ReplayResult,
    strategy_names: list[str],
) -> str:
    passes, failures = meets_live_gate(metrics)
    out: dict[str, Any] = {
        "metrics": asdict(metrics),
        "strategies": strategy_names,
        "live_gate_passes": passes,
        "live_gate_failures": failures,
        "duration_sec": result.duration_sec,
        "start_ts": result.start_ts.isoformat() if result.start_ts else None,
        "end_ts": result.end_ts.isoformat() if result.end_ts else None,
        "final_nav_usd": result.final_nav,
        "trade_count": metrics.trade_count,
        "equity_curve": result.equity_curve,
    }
    return json.dumps(out, indent=2, default=str)


def write_report(
    *,
    out_dir: Path,
    title: str,
    metrics: PerformanceMetrics,
    result: ReplayResult,
    strategy_names: list[str],
    slug: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{slug}.md"
    json_path = out_dir / f"{slug}.json"
    md_path.write_text(
        report_markdown(
            title=title,
            metrics=metrics,
            result=result,
            strategy_names=strategy_names,
            extra=extra,
        ),
        encoding="utf-8",
    )
    json_path.write_text(
        report_json(metrics=metrics, result=result, strategy_names=strategy_names),
        encoding="utf-8",
    )
    return md_path, json_path
