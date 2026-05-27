"""Risk limit checks. See MASTER_SPEC §15.2.

Each function returns either None (pass) or a string reason (block). The
RiskPolicy aggregates these into the final decision.
"""
from __future__ import annotations

from dataclasses import dataclass

from poly_meridian.domain import AggregatedSignal, PortfolioSnapshot


@dataclass(frozen=True)
class RiskLimits:
    """All limits expressed as fractions of bankroll except where noted."""

    kelly_fraction: float = 0.25
    max_position_pct_of_bankroll: float = 0.05
    max_exposure_per_category_pct: float = 0.30
    max_total_exposure_pct: float = 0.80
    daily_max_loss_pct: float = 0.05
    weekly_max_loss_pct: float = 0.10
    max_concentration_single_event_pct: float = 0.10
    max_open_positions: int = 50
    min_market_liquidity_usd: float = 10_000.0
    max_position_pct_of_market_volume: float = 0.05
    # Time-to-resolution gate. Prediction markets aren't stocks — sitting on
    # a 3-month-out position locks up capital with high variance and no
    # compounding. Default skips markets resolving > 45 days out. Set to
    # None / very large to disable.
    max_resolution_days: float | None = 45.0
    min_resolution_days: float | None = 0.5    # don't trade <12h to settlement
    # Phase Q.3 — concentration guards. Without these the system happily
    # buys YES *and* NO on the same condition (self-hedge → guaranteed
    # net loss after fees) and piles all 4 positions into one event
    # (single-resolution risk eats whole NAV). Cap per condition (HARD)
    # and per event (soft).
    max_positions_per_condition: int = 1   # YES OR NO on a condition, never both
    max_positions_per_event: int = 2       # related Polymarket markets share event_id
    # Phase Q.6b — price floor/ceiling. At <$0.05 the bid-ask spread on
    # Polymarket is routinely 30-50% of the contract price (e.g. Aramco
    # YES at $0.002 with $0.003 ask = -33% lost on entry). At >$0.95 the
    # remaining upside is <5% so risk-reward is asymmetric the wrong way.
    # Both ends kill EV regardless of how well-calibrated the signal is.
    min_entry_price: float = 0.05
    max_entry_price: float = 0.95


def check_market_liquidity(signal: AggregatedSignal, limits: RiskLimits) -> str | None:
    # Phase N.2 — FAIL CLOSED on unknown liquidity. Previously this returned
    # None ("trust signal"), but in production the aggregator almost always
    # passes market_liquidity_usd=None, so the liquidity gate was fully
    # bypassed for hours of trading. If we don't know liquidity, we don't
    # trade — operator can flip min_market_liquidity_usd to 0 if they
    # want to disable the check explicitly.
    if signal.market_liquidity_usd is None:
        return "liquidity_unknown"
    if signal.market_liquidity_usd < limits.min_market_liquidity_usd:
        return f"liquidity_below_min:{signal.market_liquidity_usd:.0f}<{limits.min_market_liquidity_usd:.0f}"
    return None


def check_daily_loss(portfolio: PortfolioSnapshot, limits: RiskLimits) -> str | None:
    if portfolio.daily_pnl_pct < -limits.daily_max_loss_pct:
        return f"daily_loss_breached:{portfolio.daily_pnl_pct:.4f}<-{limits.daily_max_loss_pct:.4f}"
    return None


def check_total_exposure(
    signal: AggregatedSignal, portfolio: PortfolioSnapshot, limits: RiskLimits
) -> str | None:
    projected = portfolio.total_exposure_pct + signal.size_pct
    if projected > limits.max_total_exposure_pct:
        return f"total_exposure_breached:{projected:.4f}>{limits.max_total_exposure_pct:.4f}"
    return None


def check_category_exposure(
    signal: AggregatedSignal, portfolio: PortfolioSnapshot, limits: RiskLimits
) -> str | None:
    if signal.category is None:
        return None
    current = portfolio.category_exposure_pct.get(signal.category, 0.0)
    projected = current + signal.size_pct
    if projected > limits.max_exposure_per_category_pct:
        return f"category_exposure_breached:{signal.category}:{projected:.4f}>{limits.max_exposure_per_category_pct:.4f}"
    return None


def check_open_position_count(portfolio: PortfolioSnapshot, limits: RiskLimits) -> str | None:
    if portfolio.open_position_count >= limits.max_open_positions:
        return f"max_open_positions_reached:{portfolio.open_position_count}>={limits.max_open_positions}"
    return None


def check_entry_price_band(
    signal: AggregatedSignal,
    limits: RiskLimits,
) -> str | None:
    """Phase Q.6b: keep entries inside the EV-viable price band.

    Outside [min_entry_price, max_entry_price] the bid-ask spread is wider
    than the predicted edge, so the math is negative before slippage.
    The strategy logic might still produce a "valid" signal there (e.g.
    stat_quant.momentum buying $0.002 lottery tickets), but the risk
    policy is the right place to enforce a hard floor — it's a
    cross-strategy constraint, not a strategy-specific one.
    """
    if signal.proposed_price is None:
        return None
    p = float(signal.proposed_price)
    if p < limits.min_entry_price:
        return f"price_below_floor:{p:.4f}<{limits.min_entry_price:.4f}"
    if p > limits.max_entry_price:
        return f"price_above_ceiling:{p:.4f}>{limits.max_entry_price:.4f}"
    return None


def check_same_condition(
    signal: AggregatedSignal,
    portfolio: PortfolioSnapshot,
    limits: RiskLimits,
    token_to_condition: dict[str, str],
) -> str | None:
    """Phase Q.3: block adding to or hedging against an existing position
    on the same Polymarket condition. The condition has two tokens (YES,
    NO) that are mutually exclusive at resolution — owning both is
    equivalent to paying fees for $1 of guaranteed-$1 payoff (net loss).
    Owning more of the same side is just position topping which we also
    skip for now (it'd defeat per-position size caps).
    """
    same = 0
    for pos in portfolio.positions:
        if float(pos.qty) <= 0:
            continue
        pos_cond = token_to_condition.get(pos.token_id)
        if pos_cond and pos_cond == signal.condition_id:
            same += 1
    if same >= limits.max_positions_per_condition:
        return f"same_condition_open:{signal.condition_id[:8]}:{same}>={limits.max_positions_per_condition}"
    return None


def check_same_event(
    signal: AggregatedSignal,
    portfolio: PortfolioSnapshot,
    limits: RiskLimits,
    token_to_event: dict[str, str],
    signal_event_id: str | None,
) -> str | None:
    """Phase Q.3: limit positions in the same Polymarket event (parent
    container that groups related condition_ids). Without this the
    system concentrates all NAV in one event's family of markets and
    a single underlying resolution drains the account."""
    if signal_event_id is None:
        return None
    same = 0
    for pos in portfolio.positions:
        if float(pos.qty) <= 0:
            continue
        ev = token_to_event.get(pos.token_id)
        if ev and ev == signal_event_id:
            same += 1
    if same >= limits.max_positions_per_event:
        return f"same_event_cap:{signal_event_id[:8]}:{same}>={limits.max_positions_per_event}"
    return None


def check_position_size_cap(signal: AggregatedSignal, limits: RiskLimits) -> str | None:
    if signal.size_pct > limits.max_position_pct_of_bankroll:
        return f"position_size_too_large:{signal.size_pct:.4f}>{limits.max_position_pct_of_bankroll:.4f}"
    return None


def reduce_size_if_breached(
    signal: AggregatedSignal, portfolio: PortfolioSnapshot, limits: RiskLimits
) -> float | None:
    """Compute the max size_pct that wouldn't breach total/category limits.

    Returns the reduced fraction, or None if no reduction needed, or 0.0 if
    even a zero-sized position would breach (caller should REJECT).
    """
    headroom_total = limits.max_total_exposure_pct - portfolio.total_exposure_pct
    headroom_total = max(0.0, headroom_total)

    if signal.category is not None:
        current_cat = portfolio.category_exposure_pct.get(signal.category, 0.0)
        headroom_cat = max(0.0, limits.max_exposure_per_category_pct - current_cat)
    else:
        headroom_cat = limits.max_total_exposure_pct  # effectively unbounded

    headroom_size_cap = limits.max_position_pct_of_bankroll

    cap = min(headroom_total, headroom_cat, headroom_size_cap)
    if cap >= signal.size_pct:
        return None
    return cap if cap > 0 else 0.0
