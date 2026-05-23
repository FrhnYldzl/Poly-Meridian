"""Promotion gate checks — paper history, metrics, drills, alerting, cap ratio."""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest


@pytest.fixture
def tmp_flag_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect FLAG_DIR to a temp dir so drill flags are isolated per test."""
    tmp = Path(tempfile.mkdtemp(prefix="promo_flags_"))
    monkeypatch.setattr("poly_meridian.promotion.FLAG_DIR", tmp)
    return tmp


def test_drill_mark_and_check(tmp_flag_dir: Path) -> None:
    from poly_meridian.promotion import drill_done, mark_drill

    assert drill_done("kill_switch") is False
    mark_drill("kill_switch")
    assert drill_done("kill_switch") is True


def test_check_drill_returns_check_result(tmp_flag_dir: Path) -> None:
    from poly_meridian.promotion import check_drill, mark_drill

    assert check_drill("backup", "DR backup").passed is False
    mark_drill("backup")
    assert check_drill("backup", "DR backup").passed is True


@pytest.mark.asyncio
async def test_paper_history_age_no_orders() -> None:
    from poly_meridian.promotion import check_paper_history_age

    class _DB:
        async def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetchval(self, q: str) -> None:
            return None

    res = await check_paper_history_age(_DB(), min_days=30)
    assert res.passed is False
    assert "no paper" in res.detail.lower()


@pytest.mark.asyncio
async def test_paper_history_age_sufficient() -> None:
    from poly_meridian.promotion import check_paper_history_age

    oldest = datetime.now(UTC) - timedelta(days=45)

    class _DB:
        async def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetchval(self, q: str) -> datetime:
            return oldest

    res = await check_paper_history_age(_DB(), min_days=30)
    assert res.passed is True
    assert res.value == 45


@pytest.mark.asyncio
async def test_initial_cap_ratio_passes_when_under_5pct() -> None:
    from poly_meridian.promotion import check_initial_cap_ratio

    class _DB:
        async def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetchval(self, q: str) -> Decimal:
            return Decimal("100000")

    res = await check_initial_cap_ratio(_DB(), proposed_live_usd=Decimal("500"))
    assert res.passed is True
    assert res.value == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_initial_cap_ratio_fails_when_over_5pct() -> None:
    from poly_meridian.promotion import check_initial_cap_ratio

    class _DB:
        async def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetchval(self, q: str) -> Decimal:
            return Decimal("10000")     # paper NAV $10K, proposing $1K = 10%

    res = await check_initial_cap_ratio(_DB(), proposed_live_usd=Decimal("1000"))
    assert res.passed is False


@pytest.mark.asyncio
async def test_alerting_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from poly_meridian.promotion import check_alerting
    from poly_meridian.settings import get_settings

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    get_settings.cache_clear()
    res = await check_alerting()
    assert res.passed is False

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/AAA/BBB/CCC")
    get_settings.cache_clear()
    res = await check_alerting()
    assert res.passed is True
    get_settings.cache_clear()


def test_promotion_report_renders_pass_when_all_checks_pass() -> None:
    from poly_meridian.promotion import CheckResult, PromotionReport

    r = PromotionReport()
    r.add(CheckResult("a", True, "ok"))
    r.add(CheckResult("b", True, "ok"))
    out = r.render()
    assert "PASS" in out
    assert r.passed is True


def test_promotion_report_renders_fail_when_any_check_fails() -> None:
    from poly_meridian.promotion import CheckResult, PromotionReport

    r = PromotionReport()
    r.add(CheckResult("a", True))
    r.add(CheckResult("b", False, "missing"))
    out = r.render()
    assert "FAIL" in out
    assert r.passed is False
