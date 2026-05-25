"""Disaster recovery drill — REAL checks, not prompts. v1.1.

MASTER_SPEC §23 + promotion-gate (§19) require evidence that DR procedures
ACTUALLY WORK before flipping to live capital. The previous version asked
the operator "did you do X?" and accepted y/N — operator could `touch
backup.ok` and pass. This version:

  - pg_dump → restore → row-count parity verification on a temp DB
  - kill-switch engage/state/disengage via real HTTP calls
  - /health probe + /api/state liveness check

Each drill writes JSON evidence to .promotion_flags/<name>.json with a
timestamp + the actual artifacts (row counts, HTTP responses). The
promotion gate requires the evidence file to be present, passed=true,
AND less than 7 days old. Old-style `.ok` files no longer suffice.

Usage:
    python -m scripts.dr_drill                    # run all drills
    python -m scripts.dr_drill backup             # run one drill
    python -m scripts.dr_drill --agent-url=URL    # override agent endpoint
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from poly_meridian.promotion import mark_drill

DEFAULT_AGENT_URL = os.environ.get(
    "AGENT_URL",
    "http://localhost:8000",
)


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else ""
    except Exception as e:
        return 0, str(e)


def _http_post(url: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else ""
    except Exception as e:
        return 0, str(e)


# ---------- Drill 1: pg_dump + restore round-trip ----------

def drill_backup(args: argparse.Namespace) -> bool:
    """Real backup → restore → row-count parity check. Requires pg_dump,
    pg_restore, createdb, dropdb on PATH and POSTGRES_URL pointing at the
    source DB. The temp restore DB is dropped on success or failure."""
    print("[1/3] Backup + restore round-trip")
    db_url = os.environ.get("POSTGRES_URL", "")
    if not db_url:
        print("  ❌ POSTGRES_URL not set")
        mark_drill("backup", passed=False, evidence={"reason": "no_postgres_url"})
        return False

    # Strip the asyncpg driver suffix that pg_dump doesn't understand.
    pg_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    # Pick tables we expect to exist + have rows.
    SAMPLE_TABLES = ("markets", "our_orders", "strategy_signals", "positions", "pnl_daily")

    def _row_counts(url: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in SAMPLE_TABLES:
            try:
                r = subprocess.run(
                    ["psql", url, "-tAc", f"SELECT COUNT(*) FROM {t}"],
                    check=True, capture_output=True, text=True, timeout=15,
                )
                counts[t] = int(r.stdout.strip() or 0)
            except Exception:
                counts[t] = -1
        return counts

    with tempfile.TemporaryDirectory() as tmp:
        dump_file = Path(tmp) / "backup.dump"

        # 1. pg_dump
        try:
            subprocess.run(
                ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
                 "-f", str(dump_file), pg_url],
                check=True, capture_output=True, text=True, timeout=120,
            )
        except subprocess.CalledProcessError as e:
            print(f"  ❌ pg_dump failed: {e.stderr[:200]}")
            mark_drill("backup", passed=False, evidence={"stage": "pg_dump", "stderr": e.stderr[:500]})
            return False
        except FileNotFoundError:
            print("  ❌ pg_dump not on PATH")
            mark_drill("backup", passed=False, evidence={"reason": "pg_dump_missing"})
            return False

        size_kb = dump_file.stat().st_size // 1024
        print(f"  ✅ pg_dump produced {size_kb} KB")

        # 2. Row counts (source)
        src_counts = _row_counts(pg_url)
        print(f"  source row counts: {src_counts}")

        # 3. Restore to temp DB
        restore_db = f"poly_meridian_drill_{int(datetime.now(UTC).timestamp())}"
        # Build admin URL that connects to `postgres` for createdb/dropdb.
        # Operator can override via DRILL_ADMIN_URL if their PG cluster uses
        # different credentials for DDL.
        admin_url = os.environ.get("DRILL_ADMIN_URL", pg_url)

        try:
            subprocess.run(
                ["createdb", "-d", admin_url.rsplit("/", 1)[0] + "/postgres", restore_db]
                if False
                else ["psql", admin_url, "-c", f"CREATE DATABASE {restore_db}"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            restored_url = pg_url.rsplit("/", 1)[0] + "/" + restore_db
            try:
                subprocess.run(
                    ["pg_restore", "--no-owner", "--no-privileges",
                     "--dbname", restored_url, str(dump_file)],
                    check=True, capture_output=True, text=True, timeout=180,
                )
                dst_counts = _row_counts(restored_url)
                print(f"  restored counts: {dst_counts}")
            finally:
                subprocess.run(
                    ["psql", admin_url, "-c", f"DROP DATABASE IF EXISTS {restore_db}"],
                    check=False, capture_output=True, text=True, timeout=30,
                )
        except subprocess.CalledProcessError as e:
            print(f"  ❌ restore stage failed: {e.stderr[:200]}")
            mark_drill("backup", passed=False, evidence={
                "stage": "restore", "stderr": e.stderr[:500],
                "src_counts": src_counts,
            })
            return False

        # 4. Verify parity
        mismatches = {
            t: (src_counts.get(t), dst_counts.get(t))
            for t in SAMPLE_TABLES
            if src_counts.get(t, -1) != dst_counts.get(t, -1)
        }
        passed = not mismatches
        mark_drill("backup", passed=passed, evidence={
            "size_kb": size_kb,
            "src_counts": src_counts,
            "dst_counts": dst_counts,
            "mismatches": mismatches,
        })
        if passed:
            print("  ✅ backup/restore parity verified")
        else:
            print(f"  ❌ row count mismatches: {mismatches}")
        return passed


# ---------- Drill 2: kill-switch engage / disengage ----------

def drill_kill_switch(args: argparse.Namespace) -> bool:
    """Engage via POST, verify /api/state shows engaged, disengage, verify."""
    print("[2/3] Kill-switch engage → state → disengage")
    base = args.agent_url.rstrip("/")
    evidence: dict = {}

    code, _ = _http_post(f"{base}/api/kill-switch/engage?reason=dr_drill")
    evidence["engage_status"] = code
    if code != 200:
        print(f"  ❌ engage POST failed: {code}")
        mark_drill("kill_switch", passed=False, evidence=evidence)
        return False

    code, body = _http_get(f"{base}/api/state")
    evidence["state_after_engage_status"] = code
    evidence["state_after_engage_ks"] = '"kill_switch_engaged":true' in body.replace(" ", "")
    if not evidence["state_after_engage_ks"]:
        print("  ❌ state did NOT reflect engaged kill-switch")
        mark_drill("kill_switch", passed=False, evidence=evidence)
        return False
    print("  ✅ engage reflected in /api/state")

    code, _ = _http_post(f"{base}/api/kill-switch/disengage")
    evidence["disengage_status"] = code
    if code != 200:
        print(f"  ❌ disengage POST failed: {code}")
        mark_drill("kill_switch", passed=False, evidence=evidence)
        return False

    code, body = _http_get(f"{base}/api/state")
    evidence["state_after_disengage_status"] = code
    evidence["state_after_disengage_ks"] = '"kill_switch_engaged":false' in body.replace(" ", "")
    passed = bool(evidence["state_after_disengage_ks"])
    mark_drill("kill_switch", passed=passed, evidence=evidence)
    if passed:
        print("  ✅ disengage reflected in /api/state")
    else:
        print("  ❌ disengage did NOT clear kill-switch state")
    return passed


# ---------- Drill 3: health + liveness ----------

def drill_health(args: argparse.Namespace) -> bool:
    """/health returns 200 + status:ok, /api/state reachable, uptime > 60s."""
    print("[3/3] Health + liveness")
    base = args.agent_url.rstrip("/")
    evidence: dict = {}

    code, body = _http_get(f"{base}/health")
    evidence["health_status"] = code
    evidence["health_body"] = body[:200]
    if code != 200 or '"ok"' not in body:
        print(f"  ❌ /health bad: {code} {body[:100]}")
        mark_drill("health", passed=False, evidence=evidence)
        return False

    code, body = _http_get(f"{base}/api/state")
    evidence["state_status"] = code
    if code != 200:
        print(f"  ❌ /api/state bad: {code}")
        mark_drill("health", passed=False, evidence=evidence)
        return False

    import json as _json
    try:
        data = _json.loads(body)
    except Exception:
        print("  ❌ /api/state body not JSON")
        mark_drill("health", passed=False, evidence=evidence)
        return False

    uptime = float(data.get("uptime_sec", 0))
    evidence["uptime_sec"] = uptime
    evidence["db_ok"] = data.get("db_ok")
    evidence["cache_ok"] = data.get("cache_ok")
    evidence["mode"] = data.get("mode")
    if uptime < 60:
        print(f"  ❌ uptime {uptime}s — agent may be crash-looping")
        mark_drill("health", passed=False, evidence=evidence)
        return False
    if not data.get("db_ok"):
        print("  ❌ db_ok=false")
        mark_drill("health", passed=False, evidence=evidence)
        return False

    mark_drill("health", passed=True, evidence=evidence)
    print(f"  ✅ uptime {uptime:.0f}s, db_ok, /health green")
    return True


# ---------- main ----------

DRILLS: dict[str, Callable[[argparse.Namespace], bool]] = {
    "backup": drill_backup,
    "kill_switch": drill_kill_switch,
    "health": drill_health,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Real DR drill — writes evidence JSON files.")
    ap.add_argument(
        "drills", nargs="*",
        help=f"Which drills to run (default: all). Options: {', '.join(DRILLS)}",
    )
    ap.add_argument("--agent-url", default=DEFAULT_AGENT_URL)
    args = ap.parse_args()

    selected = args.drills or list(DRILLS.keys())
    print("=" * 60)
    print(f"  Poly Meridian — DR Drill v1.1  ·  {datetime.now(UTC).isoformat()}")
    print(f"  agent: {args.agent_url}  ·  selected: {selected}")
    print("=" * 60)

    fails: list[str] = []
    for name in selected:
        fn = DRILLS.get(name)
        if fn is None:
            print(f"  ⚠ unknown drill: {name}")
            fails.append(name)
            continue
        print()
        try:
            ok = fn(args)
        except Exception as exc:
            print(f"  ❌ {name} raised: {exc}")
            fails.append(name)
            continue
        if not ok:
            fails.append(name)

    print("\n" + "=" * 60)
    if fails:
        print(f"DR DRILL FAILED — {len(fails)}/{len(selected)} did NOT pass:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print(f"DR DRILL PASSED — all {len(selected)} drills verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
