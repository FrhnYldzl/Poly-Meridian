"""Per-category fee schedule. See MASTER_SPEC §2.2.

Polymarket Intl taker fees vary by category and price-from-50¢. Makers
always pay 0 on Intl. Polymarket US is flat 0.30% taker, 0.20% maker rebate.

We implement the conservative worst-case (taker_bps at 50¢) for paper-fill
accounting. A more elaborate model that scales with `|price - 0.5|` lives
behind `fee_bps_for_price()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FeeSchedule:
    """Default Intl Polymarket schedule (§2.2 Mart 2026 snapshot)."""

    taker_bps_by_category: dict[str, float]
    maker_bps: float = 0.0       # Intl: makers free
    us_taker_bps: float = 30.0   # Polymarket US flat
    us_maker_rebate_bps: float = 20.0

    @classmethod
    def default(cls) -> "FeeSchedule":
        # Values from §2.2 — basis points (bps = 0.01%).
        return cls(
            taker_bps_by_category={
                "Crypto":      180.0,
                "Mentions":    156.0,
                "Economics":   150.0,
                "Culture":     125.0,
                "Weather":     125.0,
                "Finance":     100.0,
                "Politics":    100.0,
                "Tech":        100.0,
                "Sports":       75.0,
                "Geopolitics":   0.0,
                "Uncategorized": 150.0,  # conservative
            }
        )

    def taker_bps(self, category: str | None) -> float:
        if category is None:
            return self.taker_bps_by_category.get("Uncategorized", 150.0)
        return self.taker_bps_by_category.get(category, 150.0)

    def fee_bps_for_price(self, category: str | None, price: float) -> float:
        """Approximate the §2.2 'fee scales with closeness to 50¢' rule.

        At 50¢ the fee is at its peak (table value). At 1¢ or 99¢ it falls
        to ~30% of peak. Linear interpolation by `|price - 0.5|`.
        """
        peak = self.taker_bps(category)
        distance = abs(price - 0.5)               # 0 at 50¢, 0.49 at edges
        scale = 1.0 - 1.4 * distance              # 1.0 at center, ~0.31 at edges
        scale = max(0.30, min(1.0, scale))
        return peak * scale

    def estimate_fee_usd(
        self,
        *,
        notional_usd: Decimal,
        category: str | None,
        is_maker: bool,
        price: float | None = None,
        us_mode: bool = False,
    ) -> Decimal:
        if is_maker:
            bps = -self.us_maker_rebate_bps if us_mode else -self.maker_bps  # negative = rebate
        else:
            if us_mode:
                bps = self.us_taker_bps
            elif price is not None:
                bps = self.fee_bps_for_price(category, price)
            else:
                bps = self.taker_bps(category)
        return (notional_usd * Decimal(str(bps / 10_000))).quantize(Decimal("0.0001"))


DEFAULT_FEES = FeeSchedule.default()
