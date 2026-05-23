"""Paper → live promotion gating script. See MASTER_SPEC §19.

Runs the live-promotion checklist as a hard gate. Refuses to flip MODE
unless every item passes. No code path bypasses this script.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CheckItem:
    name: str
    description: str
    check: Callable[[], bool]


def _todo(label: str) -> Callable[[], bool]:
    def _fn() -> bool:
        print(f"  [MANUAL] confirm: {label}")
        return False  # placeholder — must be wired up in Phase 6
    return _fn


CHECKLIST: list[CheckItem] = [
    CheckItem("paper_30d", "Paper trading 30+ days successful", _todo("30d paper history")),
    CheckItem("sharpe_paper", "Paper Sharpe > 1.2", _todo("Sharpe ratio")),
    CheckItem("max_dd_paper", "Paper Max DD < 20%", _todo("Max drawdown")),
    CheckItem("kill_switch_drill", "Kill-switch tested at least once", _todo("kill-switch drill")),
    CheckItem("reconnect_drill", "24h+ reconnect/restart drill done", _todo("uptime drill")),
    CheckItem("secrets_rotation", "Wallet+secret rotation procedure run", _todo("secret rotation")),
    CheckItem("alerts_live", "Slack/Telegram alerting live", _todo("alerting")),
    CheckItem("dr_drill", "Backup + DB recovery tested", _todo("DR drill")),
    CheckItem("legal_review", "Regulatory/geographic posture reviewed", _todo("legal review")),
    CheckItem("initial_cap_cap", "Initial live capital <= 5% of paper NAV", _todo("capital cap")),
]


def main() -> int:
    print("Poly Meridian — promote_to_live checklist (§19)")
    print("=" * 60)
    failures: list[str] = []
    for item in CHECKLIST:
        print(f"\n[{item.name}] {item.description}")
        if not item.check():
            failures.append(item.name)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAIL — {len(failures)} of {len(CHECKLIST)} items not confirmed:")
        for f in failures:
            print(f"  - {f}")
        print("\nLive promotion REJECTED.")
        return 1

    print("PASS — all items confirmed.")
    print("To flip MODE you still need to set the env var explicitly; this")
    print("script does not modify .env on its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
