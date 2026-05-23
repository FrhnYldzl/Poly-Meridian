"""Risk engine — Kelly sizing, exposure limits, kill-switch. See MASTER_SPEC §15."""
from poly_meridian.risk.kelly import KellyResult, kelly_fraction, sized_kelly
from poly_meridian.risk.kill_switch import KillReason, KillSwitch, KillSwitchConfig
from poly_meridian.risk.limits import RiskLimits, reduce_size_if_breached
from poly_meridian.risk.policy import DefaultRiskPolicy, RiskDecision, RiskPolicy

__all__ = [
    "DefaultRiskPolicy",
    "KellyResult",
    "KillReason",
    "KillSwitch",
    "KillSwitchConfig",
    "RiskDecision",
    "RiskLimits",
    "RiskPolicy",
    "kelly_fraction",
    "reduce_size_if_breached",
    "sized_kelly",
]
