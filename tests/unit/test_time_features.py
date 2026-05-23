"""Time-to-resolution feature edge cases."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poly_meridian.features import time_features as tf


def test_hours_positive_when_future() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    end = now + timedelta(hours=24)
    assert tf.time_to_resolution_hours(now, end) == pytest.approx(24.0)


def test_hours_negative_when_past() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    end = now - timedelta(hours=1)
    h = tf.time_to_resolution_hours(now, end)
    assert h is not None and h < 0


def test_log_time_to_resolution_none_for_past() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    end = now - timedelta(hours=1)
    assert tf.log_time_to_resolution(now, end) is None


def test_decay_clamped_zero_to_one() -> None:
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    far = now + timedelta(hours=10_000)
    soon = now + timedelta(hours=1)
    past = now - timedelta(hours=1)
    assert tf.time_decay_factor(now, far) == pytest.approx(0.0, abs=0.01)
    d_soon = tf.time_decay_factor(now, soon, 168.0)
    assert d_soon is not None and 0.9 < d_soon < 1.0
    assert tf.time_decay_factor(now, past) == pytest.approx(1.0)
    assert tf.time_decay_factor(now, None) is None
