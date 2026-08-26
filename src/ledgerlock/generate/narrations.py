"""Adjustment narrations, split into the two classes T3 has to tell apart.

An adjustment has no order reference, so it can never be matched structurally.
The *only* signal is whether its narration explains what the adjustment is for
well enough for a finance team to book it. That is a language judgement, which
is the one place in this pipeline a model earns its keep.

Both lists are deliberately hard. Some benign narrations look like codes; some
opaque ones read like real categories. If the two classes were obviously
different, a keyword list would do the job and the model would be decoration.

A first version of this file had 5 opaque and 2 benign strings, and the model
scored 7/7 on them -- which proved nothing, because those 7 strings *were* the
entire vocabulary. Testing a classifier on the only inputs it will ever see is
a weak test wearing a strong test's clothes. See D15 in DECISIONS.md.
"""

from __future__ import annotations

# Explains its own purpose. A reviewer could book these without asking anyone.
# Flagging one of these is a false alarm on a clean record.
BENIGN_ADJUSTMENTS: tuple[str, ...] = (
    "Goodwill credit for support ticket",
    "Recovery of excess payout",
    "Reversal of duplicate settlement dated 12 June",
    "Refund of chargeback fee after dispute won",
    "Credit for gateway downtime on 4 June",
    "Recovery of short-collected MDR on card volume",
    "Rolling reserve released early on merchant request",
    "Compensation for delayed settlement, approved by ops",
    "Write-back of GST charged twice on fee invoice",
    "Adjustment for pricing revision effective 1 June",
    # Hard: mostly a code, but it names a checkable reference.
    "CR ADJ - see support ticket 4471",
    "Settlement shortfall reimbursed after audit",
)

# Names no reason. An internal reference, a code, or generic filler. These are
# the E12 population; missing one is an undetected exception.
OPAQUE_ADJUSTMENTS: tuple[str, ...] = (
    "MISC DR REF 88213",
    "ADJ-BATCH-CORR-Q2",
    "MANUAL ENTRY 4471 / NO REF",
    "RECON DIFF WRITEOFF",
    "PLATFORM ADJ 0091",
    "TXN ADJ / BATCH 77",
    "DR ENTRY - NO NARRATION PROVIDED",
    "REF 0092841 ADJ",
    "OPS ADJ FINAL",
    "ENTRY REVERSED - CODE 14",
    # Hard: single words that read like a category but explain nothing.
    "ADJUSTMENT",
    "CORRECTION",
    "MISCELLANEOUS",
    # Hard: looks structured, still says nothing about why.
    "SETTLEMENT CORR 2026-06",
    # Hard: real Indian banking idiom for a balancing entry, and still no
    # answer to "for what?".
    "BAL SQUARE OFF",
)

assert not set(BENIGN_ADJUSTMENTS) & set(OPAQUE_ADJUSTMENTS)
