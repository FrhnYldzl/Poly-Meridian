"""Paper → live promotion entrypoint. See MASTER_SPEC §19.

Wraps `poly_meridian.promotion.run_gate()` — the real gate logic lives in
the package so it's testable.

Usage:
    docker compose run --rm agent python -m scripts.promote_to_live \
        --proposed-live-usd 500 --min-paper-days 30

Or via the CLI:
    docker compose run --rm agent poly-meridian promote-to-live \
        --proposed-live-usd 500
"""
from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

import typer

from poly_meridian.observability.logging_config import configure_logging
from poly_meridian.promotion import mark_drill, run_gate
from poly_meridian.storage import close_db, get_db

app = typer.Typer(no_args_is_help=False, add_completion=False)


@app.command()
def run(
    proposed_live_usd: float = typer.Option(500.0, help="Initial live capital target ($)"),
    min_paper_days: int = typer.Option(30, help="Minimum days of paper history"),
    min_sharpe: float = typer.Option(1.2),
    max_drawdown: float = typer.Option(0.20),
) -> None:
    """Run the §19 promotion gate. Exit 0 on PASS, 1 on FAIL."""
    configure_logging("INFO")

    async def _run() -> int:
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
        print(report.render())
        return 0 if report.passed else 1

    sys.exit(asyncio.run(_run()))


@app.command("mark-drill")
def mark(
    name: str = typer.Argument(
        ..., help="Drill name: kill_switch | reconnect | secrets | backup | legal"
    ),
) -> None:
    """Mark a drill as completed (writes a flag under .promotion_flags/)."""
    flag = mark_drill(name)
    typer.echo(f"Marked drill complete: {flag}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
