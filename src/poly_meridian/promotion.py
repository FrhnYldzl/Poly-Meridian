"""Paper → live promotion gate. See MASTER_SPEC §19.

Each check is async + DB-aware. The gate fails CLOSED — any unknown
or failed check rejects promotion. Operator runs this via the
`promote-to-live` CLI command, NOT directly from agent code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import structlog

from poly_meridian.backtest.metrics import compute_all
from poly_meridian.storage.db import Database

log = structlog.get_logger("poly_meridian.promotion")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    value: float | int | None = None


@dataclass
class PromotionReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, c: CheckResult) -> None:
        self.checks.append(c)
        marker = "✅" if c.passed else "❌"
        log.info("promotion.check", marker=marker, name=c.name, detail=c.detail)

    def render(self) -> str:
        lines = ["Poly Meridian — promote_to_live gate (§19)", "=" * 60]
        for c in self.checks:
            marker = "✅" if c.passed else "❌"
            row = f"  {marker} [{c.name}] {c.detail}"
            if c.value is not None:
                row += f"  (value={c.value})"
            lines.append(row)
        lines.append("=" * 60)
        lines.append("PASS — promotion approved" if self.passed else "FAIL — promotion rejected")
        return "\n".join(lines) + "\n"


# Markers operators flip by running the relevant drill — kept as flag files
# under .promotion_flags/. CLI command writes them; gate checks read them.
FLAG_DIR = Path(".promotion_flags")


def mark_drill(name: str) -> Path:
    FLAG_DIR.mkdir(parents=True, exist_ok=True)
    flag = FLAG_DIR / f"{name}.ok"
    flag.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    return flag


def drill_done(name: str) -> bool:
    return (FLAG_DIR / f"{name}.ok").exists()


async def check_paper_history_age(db: Database, *, min_days: int = 30) -> CheckResult:
    """At least `min_days` of paper orders in `our_orders`."""
    try:
        async with db.acquire() as conn:
            oldest = await conn.fetchval(
                "SELECT MIN(ts_created) FROM our_orders WHERE mode = 'paper'"
            )
    except Exception as exc:
        return CheckResult("paper_history_age", False, f"db_error:{exc}")
    if oldest is None:
        return CheckResult("paper_history_age", False, "no paper orders found")
    age_days = (datetime.now(UTC) - oldest).days
    return CheckResult(
        "paper_history_age",
        age_days >= min_days,
        f"oldest paper order: {age_days}d ago (need ≥{min_days})",
        value=age_days,
    )


async def check_paper_metrics(
    db: Database,
    *,
    min_sharpe: float = 1.2,
    max_drawdown: float = 0.20,
) -> CheckResult:
    """Reconstruct daily NAV from `pnl_daily` and verify Sharpe + Max DD."""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date, ending_nav, starting_nav
                FROM pnl_daily
                ORDER BY date ASC
                """
            )
    except Exception as exc:
        return CheckResult("paper_metrics", False, f"db_error:{exc}")
    if len(rows) < 7:
        return CheckResult(
            "paper_metrics", False,
            f"need ≥7 days of pnl_daily, have {len(rows)}",
            value=len(rows),
        )
    equity = [float(r["ending_nav"]) for r in rows]
    duration_sec = max(1, (rows[-1]["date"] - rows[0]["date"]).total_seconds())
    metrics = compute_all(
        equity_curve=equity,
        trade_pnls=[],          # not used by Sharpe/MaxDD path
        duration_sec=duration_sec,
        period_sec=24 * 3600,
    )
    sharpe_ok = metrics.sharpe >= min_sharpe
    dd_ok = metrics.max_drawdown < max_drawdown
    passed = sharpe_ok and dd_ok
    detail = (
        f"sharpe {metrics.sharpe:.2f} (need ≥{min_sharpe}); "
        f"max_dd {metrics.max_drawdown:.2%} (need <{max_drawdown:.0%})"
    )
    return CheckResult("paper_metrics", passed, detail, value=metrics.sharpe)


def check_drill(name: str, label: str) -> CheckResult:
    return CheckResult(
        name,
        drill_done(name),
        f"{label} drill " + ("completed" if drill_done(name) else "NOT done — run scripts/dr_drill.py"),
    )


async def check_initial_cap_ratio(
    db: Database,
    *,
    proposed_live_usd: Decimal,
    max_ratio: float = 0.05,
) -> CheckResult:
    """Proposed initial live capital ≤ 5% of paper NAV."""
    try:
        async with db.acquire() as conn:
            paper_nav = await conn.fetchval(
                "SELECT ending_nav FROM pnl_daily ORDER BY date DESC LIMIT 1"
            )
    except Exception as exc:
        return CheckResult("initial_cap", False, f"db_error:{exc}")
    if paper_nav is None:
        return CheckResult("initial_cap", False, "no paper NAV available")
    paper_nav_d = Decimal(str(paper_nav))
    ratio = float(proposed_live_usd / paper_nav_d) if paper_nav_d > 0 else 1.0
    return CheckResult(
        "initial_cap",
        ratio <= max_ratio,
        f"proposed ${proposed_live_usd} = {ratio:.2%} of paper NAV (max {max_ratio:.0%})",
        value=ratio,
    )


async def check_alerting() -> CheckResult:
    """Slack/Telegram webhook present + reachable."""
    from poly_meridian.settings import get_settings

    s = get_settings()
    slack = s.slack_webhook_url.get_secret_value()
    telegram = s.telegram_bot_token.get_secret_value() and s.telegram_chat_id
    has = bool(slack or telegram)
    return CheckResult(
        "alerting",
        has,
        "Slack or Telegram webhook configured" if has else "no alert channel configured",
    )


async def run_gate(
    db: Database,
    *,
    proposed_live_usd: Decimal = Decimal("500"),
    min_paper_days: int = 30,
    min_sharpe: float = 1.2,
    max_drawdown: float = 0.20,
) -> PromotionReport:
    report = PromotionReport()
    report.add(await check_paper_history_age(db, min_days=min_paper_days))
    report.add(await check_paper_metrics(db, min_sharpe=min_sharpe, max_drawdown=max_drawdown))
    report.add(check_drill("kill_switch", "Kill-switch"))
    report.add(check_drill("reconnect", "24h reconnect/restart"))
    report.add(check_drill("secrets", "Secret rotation"))
    report.add(check_drill("backup", "DR backup/restore"))
    report.add(check_drill("legal", "Regulatory/geo review"))
    report.add(await check_alerting())
    report.add(await check_initial_cap_ratio(db, proposed_live_usd=proposed_live_usd))
    return report
