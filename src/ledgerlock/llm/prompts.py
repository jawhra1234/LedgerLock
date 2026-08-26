"""Prompt builders and their response schemas.

Separate module so a prompt is testable as text: `test_prompts_leak_no_ground_truth`
asserts that nothing from `data/truth/` can reach a model, and the builders are
pure functions over records the pipeline is already allowed to read.

Every schema demands a confidence and an evidence field. A model that cannot
quote what drove its answer has not given a usable answer in this domain.
"""

from __future__ import annotations

from ..domain.money import fmt

# ---------------------------------------------------------------------------
# Job A: is an adjustment's narration self-explanatory?
# ---------------------------------------------------------------------------

ADJUSTMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "explains_purpose": {"type": "boolean"},
        "category": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["explains_purpose", "category", "confidence", "evidence"],
}

_ADJUSTMENT = """You are auditing a payment gateway's settlement ledger for a merchant.

This adjustment has NO order reference, so it cannot be matched to a sale. The
only question is whether its narration explains what the adjustment is for.

explains_purpose = true   the narration names a real, bookable reason a finance
                          team could post to an account without asking anyone
explains_purpose = false  the narration is an internal reference, a code, or
                          generic filler that names no reason

Judge only the narration. Do not infer a reason from the amount or the date.
A narration that looks structured or official but still does not say *why* the
money moved is false. Quote only words that appear in the narration as evidence.

Narration: "{narration}"
Direction: {direction}
Amount: {amount}
Settled in: {settlement}
"""


def adjustment_prompt(narration: str, net: int, settlement_id: str | None) -> str:
    return _ADJUSTMENT.format(
        narration=narration or "",
        direction="credit to the merchant" if net >= 0 else "debit from the merchant",
        amount=fmt(abs(net)),
        settlement=settlement_id or "not yet settled",
    )


# ---------------------------------------------------------------------------
# Job B: two payouts of the same size, one bank credit
# ---------------------------------------------------------------------------

TIEBREAK_SCHEMA = {
    "type": "object",
    "properties": {
        "best_candidate_id": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
        "can_decide": {"type": "boolean"},
    },
    "required": ["best_candidate_id", "confidence", "evidence", "can_decide"],
}

_TIEBREAK = """A merchant's bank credit has to be matched to one gateway payout.

Several payouts have the SAME amount inside the date window, so the amount
cannot tell them apart. The only remaining signal is the reference text.

Compare the bank narration against each candidate's reference. Set
can_decide = false unless the text gives a genuine reason to prefer one -- a
partially corrupted reference that still shares a recognisable shape with one
candidate counts, a coin flip does not.

Your answer is a suggestion for a human reviewer. It will not be posted.

Bank line {line_id}, {amount} on {value_date}
Narration: "{narration}"

Candidates:
{candidates}
"""


def tiebreak_prompt(line_id: str, amount: int, value_date, narration: str,
                    candidates: list[tuple[str, str | None]]) -> str:
    rows = "\n".join(
        f"  - {sid}: reference {utr!r}" for sid, utr in candidates
    ) or "  (none)"
    return _TIEBREAK.format(
        line_id=line_id, amount=fmt(amount), value_date=value_date,
        narration=narration or "", candidates=rows,
    )


# ---------------------------------------------------------------------------
# Job C: plain English for the exception queue
# ---------------------------------------------------------------------------

EXPLANATION_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "next_step": {"type": "string"},
    },
    "required": ["explanation", "next_step"],
}

_EXPLAIN_GROUP = """Write a note for a finance team's reconciliation queue.

{count} item(s) in this cycle share one cause. Explain in ONE sentence what
happened, in plain English, for a reader who does not know the system. Then give
one short next step. Do not invent numbers or causes beyond the facts below.

Exception type: {label}
What it means: {description}
Automated handling: {action}
Total amount involved: {amount}
Example evidence: {evidence}
"""


def group_explanation_prompt(label: str, description: str, action: str,
                             count: int, amount: int, evidence: str) -> str:
    return _EXPLAIN_GROUP.format(
        label=label, description=description, action=action, count=count,
        amount=fmt(amount), evidence=(evidence or "")[:400],
    )


_EXPLAIN_ONE = """Write one line for a finance team's reconciliation queue.

Explain in plain English what is wrong with this specific record and what to do
about it. One sentence each. Use only the facts given; do not invent a cause.

Record: {subject}
Exception: {label}
Amount involved: {amount}
Machine evidence: {detail}
"""


def item_explanation_prompt(subject: str, label: str, amount: int, detail: str) -> str:
    return _EXPLAIN_ONE.format(
        subject=subject, label=label, amount=fmt(amount),
        detail=(detail or "")[:400],
    )
