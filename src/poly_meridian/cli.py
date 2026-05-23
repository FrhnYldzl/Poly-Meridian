"""CLI — typer entrypoints for run, backtest, status, db. See §10."""
from __future__ import annotations

import typer

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
def backtest() -> None:
    """Run the backtest engine (stub — Phase 4)."""
    typer.echo("backtest engine arrives in Phase 4. See MASTER_SPEC §18.")


if __name__ == "__main__":
    app()
