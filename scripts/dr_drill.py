"""Disaster recovery drill — operator runbook. See MASTER_SPEC §23.

Walks through the operator-side procedure for:
  1. Database backup (pg_dump to a tarball)
  2. Database restore (pg_restore to a fresh DB)
  3. Secret rotation
  4. Kill-switch manual engage/disengage
  5. Health probe verification

This script is **interactive** — each step prompts the operator. The point
is rehearsal, not automation. Phase 6 live promotion requires running it
end-to-end at least once.

Usage:
    docker compose run --rm agent python -m scripts.dr_drill
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DrillStep:
    name: str
    description: str
    action: Callable[[], bool]


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"  [y/N] {prompt}: ").strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}


def _step_backup_db() -> bool:
    print("STEP 1 — pg_dump backup")
    out_dir = Path("backups")
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"poly_meridian-{stamp}.dump"
    db_url = os.environ.get("POSTGRES_URL", "postgresql://poly:polypass@db:5432/poly_meridian")
    pg_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    print(f"  Target: {out_file}")
    if not _confirm("Run pg_dump?"):
        return False
    try:
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
             "--file", str(out_file), pg_url],
            check=True,
        )
    except FileNotFoundError:
        print("  pg_dump not available in this container — skipping (would run in prod).")
        return False
    print(f"  ✅ Backup written: {out_file} ({out_file.stat().st_size // 1024} KB)")
    return True


def _step_restore_check() -> bool:
    print("STEP 2 — restore verification (dry-run)")
    print("  Procedure (manual):")
    print("    1. createdb poly_meridian_restore_test")
    print("    2. pg_restore --dbname=poly_meridian_restore_test backups/<latest>.dump")
    print("    3. psql -c 'SELECT count(*) FROM markets' poly_meridian_restore_test")
    print("    4. dropdb poly_meridian_restore_test")
    return _confirm("Have you verified the restore procedure works in your environment?")


def _step_secret_rotation() -> bool:
    print("STEP 3 — secret rotation rehearsal")
    print("  Required rotation procedure (Polymarket private key + API keys):")
    print("    1. Generate new wallet (offline).")
    print("    2. Move USDC from old wallet to new (test with $10 first).")
    print("    3. Update POLYMARKET_PRIVATE_KEY in Railway env vars.")
    print("    4. Trigger CLOB derive_api_key on first boot.")
    print("    5. Confirm old wallet is empty + decommission.")
    return _confirm("Have you rehearsed this on a testnet/devnet?")


def _step_kill_switch_test() -> bool:
    print("STEP 4 — kill-switch engage/disengage")
    print("  Procedure:")
    print("    1. Manually trigger via CLI: python -m poly_meridian.cli status (verify mode)")
    print("    2. Engage via Slack webhook or admin endpoint (Phase 6 wiring).")
    print("    3. Confirm pm_kill_switch_engaged metric = 1.")
    print("    4. Confirm no new orders submitted for 5 minutes.")
    print("    5. Disengage. Confirm metric = 0 + orders resume.")
    return _confirm("Have you exercised the kill-switch in paper mode?")


def _step_health_check() -> bool:
    print("STEP 5 — /health probe + Grafana alert")
    print("  - Curl http://localhost:8000/health → expect {\"status\":\"ok\"}")
    print("  - Confirm Grafana Overview shows green NAV, kill-switch ARMED.")
    print("  - Confirm Prometheus is scraping pm_* metrics.")
    return _confirm("All health probes green?")


CHECKLIST: list[DrillStep] = [
    DrillStep("backup", "pg_dump → tarball", _step_backup_db),
    DrillStep("restore", "Verify restore works", _step_restore_check),
    DrillStep("secrets", "Secret rotation procedure", _step_secret_rotation),
    DrillStep("kill_switch", "Engage / disengage drill", _step_kill_switch_test),
    DrillStep("health", "/health + Grafana", _step_health_check),
]


def main() -> int:
    print("=" * 60)
    print("  Poly Meridian — Disaster Recovery Drill")
    print("=" * 60)
    print()
    fails: list[str] = []
    for s in CHECKLIST:
        print(f"\n--- {s.name}: {s.description} ---")
        try:
            ok = s.action()
        except Exception as exc:
            print(f"  ❌ {s.name}: {exc}")
            fails.append(s.name)
            continue
        if not ok:
            fails.append(s.name)
            print(f"  ❌ {s.name}: not confirmed")
        else:
            print(f"  ✅ {s.name}")

    print("\n" + "=" * 60)
    if fails:
        print(f"DR DRILL FAILED: {len(fails)} of {len(CHECKLIST)} steps not confirmed:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("DR DRILL PASSED — all 5 steps confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
