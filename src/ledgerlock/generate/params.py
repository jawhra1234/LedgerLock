"""Scenario specification. A profile plus a seed fully determines the dataset."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..domain.taxonomy import ExceptionCode as EC


class Basis(StrEnum):
    """What population a code's injection rate is measured against.

    Not cosmetic. Order-level faults scale with traffic; bank-level faults
    scale with the number of payouts, which is driven by business days rather
    than by order count. Expressing everything per-order makes bank faults
    explode on large profiles -- see F7 in DECISIONS.md.
    """
    ORDERS = "orders"
    SETTLEMENTS = "settlements"


RATE_BASIS: dict[EC, Basis] = {
    # Order-level: more traffic means proportionally more of these.
    EC.DUPLICATE_PAYMENT: Basis.ORDERS,
    EC.MISSING_IN_PG: Basis.ORDERS,
    EC.ORPHAN_PG_ENTRY: Basis.ORDERS,
    EC.ROUNDING_DRIFT: Basis.ORDERS,
    EC.MATERIAL_MISMATCH: Basis.ORDERS,
    EC.TIMING_UNSETTLED: Basis.ORDERS,        # label-only; rate unused
    EC.CROSS_CYCLE_REFUND: Basis.ORDERS,      # label-only; rate unused
    # Payout-level: a bank mangling a UTR or batching two credits does so per
    # payout. Ten times the orders does not mean ten times the clumsy
    # narrations -- it means the same bank handling bigger payouts.
    EC.UTR_CORRUPTED: Basis.SETTLEMENTS,
    EC.NON_PG_INFLOW: Basis.SETTLEMENTS,
    EC.MERGED_SETTLEMENT: Basis.SETTLEMENTS,
    EC.SPLIT_SETTLEMENT: Basis.SETTLEMENTS,
    EC.UNEXPLAINED_ADJUSTMENT: Basis.SETTLEMENTS,
}

# Every code is floored at 1 occurrence, so even the 50-order smoke profile
# exercises all twelve branches of the pipeline.
# E06 and E07 are absent on purpose: they are label-only codes covering the
# whole natural pool, not injected at a rate. See injectors.e06_timing_unsettled.
DEFAULT_RATES: dict[EC, float] = {
    EC.DUPLICATE_PAYMENT: 0.010,
    EC.MISSING_IN_PG: 0.012,
    EC.ORPHAN_PG_ENTRY: 0.008,
    EC.ROUNDING_DRIFT: 0.030,
    EC.MATERIAL_MISMATCH: 0.006,
    EC.UTR_CORRUPTED: 0.060,
    EC.NON_PG_INFLOW: 0.080,
    EC.MERGED_SETTLEMENT: 0.050,
    EC.SPLIT_SETTLEMENT: 0.050,
    EC.UNEXPLAINED_ADJUSTMENT: 0.150,
}

# A bank that mangles or merges most of a merchant's payouts is a fiction, and
# scoring a matcher against fiction proves nothing. Pinned by
# test_bank_faults_stay_plausible_at_every_scale.
MAX_UTR_PATH_BROKEN = 0.15


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

    def count_for(self, code: EC, n_settlements: int = 0) -> int:
        """How many of `code` to inject, measured against the right population."""
        basis = RATE_BASIS[code]
        population = n_settlements if basis is Basis.SETTLEMENTS else self.n_orders
        return max(1, round(self.rates.get(code, 0.0) * population))


PROFILES: dict[str, ScenarioSpec] = {
    # 28 days rather than 14: with only 6 payouts the >=1-per-code floor put a
    # bank fault on a third of them, which is not a bank. A wider window gives
    # ~17 payouts, so the floor stops distorting the realism ceiling while the
    # order count still sits at the stated 50-record bar.
    "smoke": ScenarioSpec("smoke", n_orders=50, n_days=28),
    "default": ScenarioSpec("default", n_orders=500, n_days=30),
    "scale": ScenarioSpec("scale", n_orders=5000, n_days=90),
}
