"""Single source of truth for every rate, tolerance and cycle parameter.

Nothing in the codebase hardcodes a rate. If a number has business meaning it
lives here, so the generator and the pipeline can never silently disagree
about, say, what the GST rate is.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# --- Money -----------------------------------------------------------------
CURRENCY = "INR"

# --- Fee engine ------------------------------------------------------------
# MDR by payment method, as a fraction of gross. UPI is genuinely zero-MDR in
# India; keeping it at 0.00 means the dataset contains fee=0 / tax=0 rows,
# which is a real edge case a matcher has to survive.
MDR_RATES: dict[str, Decimal] = {
    "card": Decimal("0.0200"),
    "netbanking": Decimal("0.0190"),
    "wallet": Decimal("0.0200"),
    "upi": Decimal("0.0000"),
}
METHOD_WEIGHTS: dict[str, float] = {
    "upi": 0.46,
    "card": 0.30,
    "netbanking": 0.14,
    "wallet": 0.10,
}

GST_RATE = Decimal("0.18")          # levied on the fee, not on the gross
TDS_RATE = Decimal("0.001")         # s.194-O, on gross of the settlement batch
CHARGEBACK_FEE_PAISE = 150_000      # Rs 1,500 flat

# Razorpay does not return the original MDR when a payment is refunded. This
# asymmetry is the single most common cause of "my books are off by the fee"
# and any matcher that assumes symmetry will fail on refunds.
REFUND_RETURNS_MDR = False

# --- Rolling reserve -------------------------------------------------------
ROLLING_RESERVE_PCT = Decimal("0.05")
RESERVE_RELEASE_DAYS = 7

# --- Settlement cycle ------------------------------------------------------
SETTLEMENT_CYCLE_DAYS = 2           # T+2, shifted forward off weekends
WORLD_START = date(2026, 6, 1)      # fixed: the generator never reads the clock

# --- Matching tolerances (consumed by the pipeline, declared here) ---------
# Anything inside this band is a rounding artefact and may be auto-resolved.
# Anything outside it is a material discrepancy and must be reported, never
# absorbed. The gap between these two lines is the entire ethic of the project.
ROUNDING_TOLERANCE_PAISE = 50       # Rs 0.50
AMOUNT_MATCH_TOLERANCE_PAISE = 0    # exact-match tier admits no slack
DATE_WINDOW_DAYS = 4                # how far a settlement may drift from T+2

# --- Bank narration ---------------------------------------------------------
# The gateway's own signature in a bank narration. This is configuration, not a
# hardcoded answer: a real controller is told what its processor's credits look
# like. A credit without this marker is somebody else's money.
PG_NARRATION_MARKERS = ("RAZORPAY",)

# --- Tier 2 policy ---------------------------------------------------------
# Confidence and action are separated. A rule states how strong its evidence
# is; this threshold decides whether that is enough to close a case without
# review. Overriding it always: a code whose resolvability is not FULL never
# auto-resolves, at any confidence.
AUTO_RESOLVE_MIN_CONFIDENCE = 0.90

# Bounds on the subset-sum search. Given a free hand it will always find you
# an answer -- some subset of enough numbers sums to almost anything -- so the
# search is capped, requires exact equality, and refuses to choose between two
# subsets that both work. Truncation is logged, never silent.
SUBSET_SUM_MAX_CANDIDATES = 12
SUBSET_SUM_MAX_SUBSET = 3

# The remitter name the gateway pays out under, as it appears on the bank
# statement. A real deployment configures this; it is what lets the reconciler
# tell "a credit we could not explain" from "a credit that was never ours".
GATEWAY_REMITTER = "RAZORPAY SOFTWARE PVT LTD"
