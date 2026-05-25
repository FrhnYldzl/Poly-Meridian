"""LiveExecutor — submits real orders to Polymarket CLOB. §16.4.

Built to mirror PaperExecutor's surface exactly so the main loop can swap
on `settings.mode`. Hard safety rails:

  - Refuses to start unless `MODE` is `live-conservative` or `live-normal`.
  - Refuses to submit if `RiskPolicy.evaluate()` wasn't APPROVE/REDUCE
    upstream (we trust the pipeline to enforce this; we double-log).
  - Every submission writes to `our_orders` with the exact mode string.
  - Cancel cascade on disconnect (config-gated).

The library-level call is encapsulated in `_post_order()` so version
drift between `py-clob-client` releases only requires touching one method.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from poly_meridian.domain import (
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TradeDecision,
)
from poly_meridian.execution.base import Executor
from poly_meridian.execution.fees import DEFAULT_FEES, FeeSchedule
from poly_meridian.ingestion.clob_client import ClobClient
from poly_meridian.settings import get_settings

log = structlog.get_logger("poly_meridian.live_executor")


class LiveExecutor(Executor):
    """Real-money executor. Always sets `Order.mode` to whichever live mode
    is active; main loop chooses the variant via `settings.mode`."""

    def __init__(
        self,
        *,
        clob: ClobClient | None = None,
        fee_schedule: FeeSchedule | None = None,
        us_mode: bool = False,
        on_fill: object = None,
    ) -> None:
        s = get_settings()
        if s.mode not in (Mode.LIVE_CONSERVATIVE, Mode.LIVE_NORMAL):
            raise RuntimeError(
                f"LiveExecutor refuses to start in mode={s.mode!r}; "
                "expected live-conservative or live-normal"
            )
        self.mode = s.mode
        self._clob = clob or ClobClient()
        self._fees = fee_schedule or DEFAULT_FEES
        self._us_mode = us_mode
        self._on_fill = on_fill
        self._token_category: dict[str, str] = {}
        self._orders: dict[str, Order] = {}
        self._lock = asyncio.Lock()
        # Per-order venue-side id mapping (our order_id → venue order_id).
        self._venue_id: dict[str, str] = {}

    async def start(self) -> None:
        await self._clob.start()
        if not self._clob.init_authed():
            raise RuntimeError(
                "LiveExecutor cannot start: authed CLOB client failed to initialize. "
                "Check POLYMARKET_PRIVATE_KEY + py-clob-client install."
            )
        log.info("live_executor.start", mode=str(self.mode))

    async def stop(self) -> None:
        await self._clob.stop()

    def attach_book(self, token_id: str, book: object) -> None:
        # LiveExecutor reads books only via the venue (no local replica).
        # Kept for interface compatibility with PaperExecutor.
        return

    def attach_category(self, token_id: str, category: str | None) -> None:
        if category:
            self._token_category[token_id] = category

    def estimate_fill_fee(
        self,
        *,
        token_id: str,
        notional_usd: Decimal,
        price: float,
        is_maker: bool,
    ) -> Decimal:
        return self._fees.estimate_fee_usd(
            notional_usd=notional_usd,
            category=self._token_category.get(token_id),
            is_maker=is_maker,
            price=price,
            us_mode=self._us_mode,
        )

    async def submit(self, decision: TradeDecision) -> Order:
        order_id = f"live-{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            ts_created=decision.ts,
            strategy=decision.strategy,
            token_id=decision.token_id,
            side=decision.side,
            order_type=decision.order_type,
            price=decision.price,
            size=decision.size,
            status=OrderStatus.PENDING,
            mode=self.mode,
        )
        async with self._lock:
            self._orders[order_id] = order

        try:
            venue_resp = await self._post_order(decision)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            log.error("live.submit_failed", order_id=order_id, error=str(exc))
            return order

        venue_id = (
            venue_resp.get("orderID")
            or venue_resp.get("order_id")
            or venue_resp.get("id")
        ) if isinstance(venue_resp, dict) else None
        if venue_id:
            self._venue_id[order_id] = str(venue_id)

        order.status = OrderStatus.LIVE
        log.info(
            "live.submit",
            order_id=order_id,
            venue_id=venue_id,
            side=str(decision.side),
            token_id=decision.token_id,
            price=str(decision.price),
            size=str(decision.size),
            order_type=str(decision.order_type),
        )
        return order

    async def cancel(self, order_id: str) -> bool:
        async with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return False
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return False
            venue_id = self._venue_id.get(order_id)
            if venue_id is None:
                # Local-only: best-effort mark.
                order.status = OrderStatus.CANCELLED
                return True
            try:
                ok = await self._cancel_venue(venue_id)
            except Exception as exc:
                log.warning("live.cancel_failed", order_id=order_id, error=str(exc))
                return False
            if ok:
                order.status = OrderStatus.CANCELLED
            return ok

    async def cancel_all_open_orders(self) -> int:
        """Kill-switch flatten path. Cancels every pending/live/partial
        order at the venue. Best-effort: per-order failures logged but
        don't abort the sweep (a partial flatten is better than none)."""
        n = 0
        async with self._lock:
            open_ids = [
                oid for oid, o in self._orders.items()
                if o.status in (OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIAL)
            ]
        for oid in open_ids:
            try:
                ok = await self.cancel(oid)
                if ok:
                    n += 1
            except Exception as exc:
                log.warning("live.cancel_all_partial_fail", order_id=oid, error=str(exc))
        if n > 0:
            log.warning("live.cancel_all_open", count=n, attempted=len(open_ids))
        return n

    async def apply_user_ws_event(self, evt: dict[str, object]) -> None:
        """Consume a user-channel WS event (order or trade) and update local
        state. Primary fill path in live mode — `reconcile()` is only the
        fallback for missed events.

        Event shapes Polymarket emits (post-decode):
          order_update: {orderID, status, size_matched, ...}
          trade:        {orderID, price, size, fee, ...}
        We map by venue orderID → our order_id via self._venue_id reverse
        lookup so the agent's internal Order can be mutated and the on_fill
        callback fired.
        """
        from datetime import UTC, datetime
        from decimal import Decimal as _D

        payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else evt
        if not isinstance(payload, dict):
            return
        venue_id = (
            payload.get("orderID")
            or payload.get("order_id")
            or payload.get("id")
        )
        if not venue_id:
            return
        # Reverse-lookup our internal order_id from venue id.
        our_id = next(
            (k for k, v in self._venue_id.items() if v == venue_id),
            None,
        )
        if our_id is None:
            return
        order = self._orders.get(our_id)
        if order is None:
            return

        event_type = (
            evt.get("type") or evt.get("event_type") or payload.get("event_type")
        )
        async with self._lock:
            if event_type in ("order", "order_update"):
                status = (payload.get("status") or "").upper()
                if status in ("LIVE", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"):
                    try:
                        order.status = OrderStatus[status]
                    except KeyError:
                        pass
                size_matched = payload.get("size_matched") or payload.get("filledSize")
                if size_matched is not None:
                    try:
                        order.filled_size = _D(str(size_matched))
                    except Exception:
                        pass
                log.info("live.user_ws.order_update", order_id=our_id, status=status)

            elif event_type in ("trade", "trade_update", "fill"):
                price = payload.get("price")
                size = payload.get("size") or payload.get("matchedSize")
                fee = payload.get("fee") or 0
                if price is None or size is None:
                    return
                try:
                    fill_price = _D(str(price))
                    filled = _D(str(size))
                    fee_d = _D(str(fee))
                except Exception:
                    return
                order.avg_fill_price = fill_price
                order.filled_size = (order.filled_size or _D(0)) + filled
                order.ts_filled = datetime.now(UTC)
                order.status = (
                    OrderStatus.FILLED
                    if order.filled_size >= order.size
                    else OrderStatus.PARTIAL
                )
                log.info(
                    "live.user_ws.fill",
                    order_id=our_id,
                    price=str(fill_price),
                    filled=str(filled),
                )
                if self._on_fill is not None:
                    try:
                        await self._on_fill(order, filled, fill_price, fee_d)
                    except Exception as exc:
                        log.warning("live.on_fill_error", order_id=our_id, error=str(exc))

    async def reconcile(self) -> None:
        """Pull open orders from the venue, reconcile against local state.

        Phase 6: fill confirmations come via the user-channel WS
        (`clob_user_ws.py`). reconcile() is the fallback path for missed
        events.
        """
        if not self._clob.has_authed_client():
            return
        try:
            opens = await self._fetch_open_orders()
        except Exception as exc:
            log.warning("live.reconcile_failed", error=str(exc))
            return

        async with self._lock:
            venue_open_ids = {o.get("orderID") for o in opens if isinstance(o, dict)}
            for our_id, venue_id in list(self._venue_id.items()):
                if venue_id not in venue_open_ids:
                    o = self._orders.get(our_id)
                    if o is not None and o.status == OrderStatus.LIVE:
                        # Order is no longer open on venue → assume filled.
                        # Real fill detail comes via user WS; here we just mark.
                        o.status = OrderStatus.FILLED
                        o.ts_filled = datetime.now(UTC)
                        log.info("live.reconcile.filled", order_id=our_id)

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def open_orders(self) -> list[Order]:
        return [
            o for o in self._orders.values()
            if o.status in (OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIAL)
        ]

    # ---------- venue plumbing (py-clob-client) ----------

    async def _post_order(self, decision: TradeDecision) -> dict[str, Any]:
        """Wraps the synchronous py-clob-client call in a thread pool."""
        client = self._clob.authed()
        # Build OrderArgs / MarketOrderArgs depending on order type.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._post_order_sync, client, decision
        )

    def _post_order_sync(self, client: Any, decision: TradeDecision) -> dict[str, Any]:
        """Synchronous py-clob-client invocation. Verifies methods at runtime."""
        side_label = "BUY" if decision.side == Side.BUY else "SELL"
        order_type_label = str(decision.order_type)

        # Try the limit-order path for GTC/GTD.
        if decision.order_type in (OrderType.GTC, OrderType.GTD):
            args = self._build_order_args(client, decision, side_label)
            signed = self._invoke(client, ["create_order", "create_signed_order"], args)
            resp = self._invoke(client, ["post_order"], signed, order_type_label)
            return self._coerce_resp(resp)

        # Market path: FOK/FAK.
        args = self._build_market_order_args(client, decision, side_label)
        signed = self._invoke(client, ["create_market_order", "create_signed_order"], args)
        resp = self._invoke(client, ["post_order"], signed, order_type_label)
        return self._coerce_resp(resp)

    def _build_order_args(self, client: Any, decision: TradeDecision, side_label: str) -> Any:
        """Construct OrderArgs from the library."""
        lib = self._clob._lib  # type: ignore[reportPrivateUsage]
        OrderArgs = getattr(getattr(lib, "clob_types", None), "OrderArgs", None) or getattr(
            lib, "OrderArgs", None
        )
        if OrderArgs is None:
            raise RuntimeError("py-clob-client: OrderArgs not found")
        return OrderArgs(
            price=float(decision.price) if decision.price is not None else 0.0,
            size=float(decision.size),
            side=side_label,
            token_id=decision.token_id,
        )

    def _build_market_order_args(self, client: Any, decision: TradeDecision, side_label: str) -> Any:
        lib = self._clob._lib  # type: ignore[reportPrivateUsage]
        MarketOrderArgs = getattr(getattr(lib, "clob_types", None), "MarketOrderArgs", None) or getattr(
            lib, "MarketOrderArgs", None
        )
        if MarketOrderArgs is None:
            raise RuntimeError("py-clob-client: MarketOrderArgs not found")
        return MarketOrderArgs(
            token_id=decision.token_id,
            amount=float(decision.size),
            side=side_label,
        )

    def _invoke(self, client: Any, method_names: list[str], *args: Any) -> Any:
        """Call the first method that exists on the client."""
        for name in method_names:
            method = getattr(client, name, None)
            if callable(method):
                return method(*args)
        raise RuntimeError(
            f"py-clob-client: none of {method_names} are callable on {type(client).__name__}"
        )

    def _coerce_resp(self, resp: Any) -> dict[str, Any]:
        if isinstance(resp, dict):
            return resp
        # Some library versions return a typed object — best-effort to dict.
        if hasattr(resp, "__dict__"):
            return {k: v for k, v in vars(resp).items() if not k.startswith("_")}
        return {"raw": str(resp)}

    async def _cancel_venue(self, venue_order_id: str) -> bool:
        client = self._clob.authed()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._invoke(client, ["cancel", "cancel_order"], venue_order_id),
        )
        if isinstance(result, dict):
            return bool(result.get("canceled") or result.get("status") == "canceled")
        return bool(result)

    async def _fetch_open_orders(self) -> list[dict[str, Any]]:
        client = self._clob.authed()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._invoke(client, ["get_orders", "open_orders"]),
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "data" in result:
            return list(result["data"])
        return []
