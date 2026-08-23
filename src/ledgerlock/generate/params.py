"""Scenario specification. A profile plus a seed fully determines the dataset."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.taxonomy import ExceptionCode as EC

# Injection rates are expressed per-order so profiles scale coherently. Every
# code is floored at 1 occurrence, so even the 50-order smoke profile exercises
# all twelve branches of the pipeline.
# E06 and E07 are absent on purpose: they are label-only codes covering the
# whole natural pool, not injected at a rate. See injectors.e06_timing_unsettled.
DEFAULT_RATES: dict[EC, float] = {
    EC.DUPLICATE_PAYMENT: 0.010,
    EC.MISSING_IN_PG: 0.012,
    EC.ORPHAN_PG_ENTRY: 0.008,
    EC.ROUNDING_DRIFT: 0.030,
    EC.MATERIAL_MISMATCH: 0.006,
    EC.UTR_CORRUPTED: 0.004,
    EC.NON_PG_INFLOW: 0.004,
    EC.MERGED_SETTLEMENT: 0.003,
    EC.SPLIT_SETTLEMENT: 0.003,
    EC.UNEXPLAINED_ADJUSTMENT: 0.006,
}


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    n_orders: int
    seed: int = 42
    n_days: int = 30
    refund_rate: float = 0.06
    chargeback_rate: float = 0.008
    order_fail_rate: float = 0.07      # normal noise, NOT an exception
    rates: dict[EC, float] = field(default_factory=lambda: dict(DEFAULT_RATES))

    def count_for(self, code: EC) -> int:
        return max(1, round(self.rates.get(code, 0.0) * self.n_orders))


PROFILES: dict[str, ScenarioSpec] = {
    "smoke": ScenarioSpec("smoke", n_orders=50, n_days=14),
    "default": ScenarioSpec("default", n_orders=500, n_days=30),
    "scale": ScenarioSpec("scale", n_orders=5000, n_days=90),
}
