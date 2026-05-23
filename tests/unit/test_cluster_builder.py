"""ClusterStateBuilder — event consumption + state snapshots."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from poly_meridian.strategies.cluster_builder import (
    ClusterStateBuilder,
    _is_receipt,
    _parse_token_id,
    _parse_value,
)


def _topic(addr: str) -> str:
    return "0x" + addr.lower().removeprefix("0x").rjust(64, "0")


def _evt(*, wallet: str, token_id_hex: str, value_hex: str, ts: datetime | None = None) -> dict[str, object]:
    return {
        "source": "onchain",
        "type": "ctf_transfer",
        "ts": ts or datetime.now(UTC),
        "wallet": wallet,
        "payload": {
            "topics": [
                "0xabc",                         # event sig
                _topic("0x" + "11" * 20),        # operator
                _topic("0x" + "22" * 20),        # from
                _topic(wallet),                  # to == wallet → receipt
            ],
            "data": "0x"
                + token_id_hex.rjust(64, "0")
                + value_hex.rjust(64, "0"),
        },
    }


def test_parse_token_id_from_topic() -> None:
    payload = {"topics": ["sig", "from", "to", _topic("0x1234")]}
    assert _parse_token_id(payload) == str(0x1234)


def test_parse_token_id_returns_none_when_topics_missing() -> None:
    assert _parse_token_id({"topics": ["a", "b"]}) is None


def test_parse_value_from_data() -> None:
    data = "0x" + "ab".rjust(64, "0") + "0a".rjust(64, "0")  # id=171, value=10
    assert _parse_value({"data": data}) == 10.0


def test_is_receipt_matches_wallet() -> None:
    wallet = "0xaaaa"
    payload = {"topics": ["sig", "a", "b", _topic(wallet)]}
    assert _is_receipt(payload, wallet) is True


def test_is_receipt_false_when_wallet_not_in_to() -> None:
    payload = {"topics": ["sig", "a", "b", _topic("0xbbbb")]}
    assert _is_receipt(payload, "0xaaaa") is False


@pytest.mark.asyncio
async def test_builder_consumes_events_into_state() -> None:
    builder = ClusterStateBuilder(window_sec=3600, decay_sec=600)
    builder.register_token_to_condition(token_id="42", condition_id="cond-1", direction="YES")
    builder.register_wallet_tier("0x" + "aa" * 20, tier=1)

    async def events() -> object:
        wallet = "0x" + "aa" * 20
        yield _evt(wallet=wallet, token_id_hex="2a", value_hex="0a")  # token=42, value=10

    await builder.start(events())
    await asyncio.sleep(0.05)
    snap = builder.snapshot_state("cond-1")
    assert snap is not None
    assert len(snap.yes_flows) == 1
    await builder.stop()


@pytest.mark.asyncio
async def test_snapshot_filters_stale_flows() -> None:
    builder = ClusterStateBuilder(window_sec=60, decay_sec=10)
    builder.register_token_to_condition(token_id="100", condition_id="c2", direction="YES")
    wallet = "0x" + "bb" * 20
    # Inject a stale flow directly.
    stale = datetime.now(UTC) - timedelta(seconds=120)
    builder._flows["c2"].append(  # type: ignore[reportPrivateUsage]
        type(builder)._flows.__getitem__(builder._flows, "c2")[:]  # no-op typecheck
        and __import__("poly_meridian.strategies.cluster_builder",
                       fromlist=["WalletFlow"])  # noqa
    )
    # Simpler: use the public WalletFlow type.
    from poly_meridian.strategies.smart_money import WalletFlow

    builder._flows["c2"].clear()  # type: ignore[reportPrivateUsage]
    builder._flows["c2"].append(WalletFlow(wallet=wallet, direction="YES", net_usd=1.0, last_update=stale))  # type: ignore[reportPrivateUsage]

    snap = builder.snapshot_state("c2")
    assert snap is None
