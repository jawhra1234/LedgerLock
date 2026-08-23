"""The exception taxonomy.

`resolvability` is the field that matters. A case marked NONE is one the
pipeline is *supposed* to leave unresolved -- correctly flagging it counts as a
success in eval, and "resolving" it counts as a false match. Without this
column, an evaluator rewards a matcher for guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Resolvability(StrEnum):
    FULL = "full"        # a correct pipeline resolves this automatically
    PARTIAL = "partial"  # resolvable to a candidate set, needs human confirmation
    NONE = "none"        # honestly unresolvable; must surface as an exception


class Tier(StrEnum):
    T1 = "t1_deterministic"
    T2 = "t2_rules"
    T3 = "t3_llm"


class ExceptionCode(StrEnum):
    DUPLICATE_PAYMENT = "E01"
    MISSING_IN_PG = "E02"
    ORPHAN_PG_ENTRY = "E03"
    ROUNDING_DRIFT = "E04"
    MATERIAL_MISMATCH = "E05"
    TIMING_UNSETTLED = "E06"
    CROSS_CYCLE_REFUND = "E07"
    UTR_CORRUPTED = "E08"
    NON_PG_INFLOW = "E09"
    MERGED_SETTLEMENT = "E10"
    SPLIT_SETTLEMENT = "E11"
    UNEXPLAINED_ADJUSTMENT = "E12"


@dataclass(frozen=True)
class ExceptionMeta:
    code: ExceptionCode
    label: str
    resolvability: Resolvability
    expected_tier: Tier
    description: str


_M = ExceptionMeta
R = Resolvability

EXCEPTION_META: dict[ExceptionCode, ExceptionMeta] = {
    m.code: m
    for m in [
        _M(ExceptionCode.DUPLICATE_PAYMENT, "Duplicate payment", R.PARTIAL, Tier.T2,
           "Same order, same amount, seconds apart, two payment ids. Only a human "
           "knows whether the customer really paid twice."),
        _M(ExceptionCode.MISSING_IN_PG, "Missing in gateway", R.FULL, Tier.T1,
           "ERP marks the order paid but the gateway has no entry for it."),
        _M(ExceptionCode.ORPHAN_PG_ENTRY, "Orphan gateway entry", R.FULL, Tier.T1,
           "Gateway entry references an order_id absent from the ERP."),
        _M(ExceptionCode.ROUNDING_DRIFT, "Fee rounding drift", R.FULL, Tier.T2,
           "Fee differs by under the rounding tolerance; absorbable, but must be "
           "reported as absorbed rather than silently dropped."),
        _M(ExceptionCode.MATERIAL_MISMATCH, "Material amount mismatch", R.NONE, Tier.T2,
           "Discrepancy beyond tolerance. There is no safe automatic answer; this "
           "is a genuine escalation."),
        _M(ExceptionCode.TIMING_UNSETTLED, "Unsettled at cut-off", R.FULL, Tier.T2,
           "Captured but its settlement cycle has not run yet. Defer, do not chase."),
        _M(ExceptionCode.CROSS_CYCLE_REFUND, "Cross-cycle refund", R.FULL, Tier.T2,
           "Refund of a payment settled in an earlier cycle, dragging this one down."),
        _M(ExceptionCode.UTR_CORRUPTED, "Corrupted UTR in narration", R.FULL, Tier.T3,
           "Bank narration mangles the UTR. Fuzzy/semantic recovery territory."),
        _M(ExceptionCode.NON_PG_INFLOW, "Non-gateway inflow", R.FULL, Tier.T2,
           "A real bank credit that is simply not gateway money. Correct outcome "
           "is 'out of scope', not 'unmatched'."),
        _M(ExceptionCode.MERGED_SETTLEMENT, "Merged settlement credit", R.FULL, Tier.T2,
           "Bank aggregated two settlements into one credit line. Subset-sum."),
        _M(ExceptionCode.SPLIT_SETTLEMENT, "Split settlement credit", R.FULL, Tier.T2,
           "One settlement arrived as two bank credits."),
        _M(ExceptionCode.UNEXPLAINED_ADJUSTMENT, "Unexplained adjustment", R.NONE, Tier.T3,
           "Adjustment with no order linkage and an opaque narration. An LLM may "
           "classify its likely nature; nothing can legitimately match it."),
    ]
}

assert set(EXCEPTION_META) == set(ExceptionCode), "taxonomy metadata is incomplete"
