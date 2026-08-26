"""Tier 3: model-assisted, on what T1 and T2 genuinely could not touch.

**T3 never proposes a link.** Not at any confidence, not with any evidence. It
emits findings and suggestions only. That is not caution for its own sake -- it
is what makes the project's central number structurally true rather than
empirically lucky: a tier that cannot create a link cannot create a false match,
so the false-match rate is guaranteed by the architecture and not by the model
behaving well on the day.

What T3 does:

  A. Decides whether an adjustment's narration explains itself. Structurally
     unmatchable either way, so nothing is being resolved -- the question is
     whether a human needs to look at it. The benign adjustments in the dataset
     are the control group, and flagging one is a false alarm.
  B. Suggests a candidate when two same-sized payouts cannot be told apart by
     amount. A suggestion for review, never a link.
  C. Writes the plain-English queue: one note per exception class, plus the
     largest individual items. Presentation over data already computed, and
     deliberately not credited by eval.
"""

from __future__ import annotations

from .. import config
from ..domain.models import EntryType
from ..domain.taxonomy import EXCEPTION_META, ExceptionCode as EC
from ..llm import prompts
from ..llm.adapter import LLMClient
from .result import Action, Finding, Tier
from .views import Index

T = Tier.T3


# ---------------------------------------------------------------------------
# Job A: opaque adjustments
# ---------------------------------------------------------------------------

def r16_classify_adjustments(ix: Index, llm: LLMClient) -> list[Finding]:
    """Ask, for every unlinked adjustment, whether its narration explains itself.

    Every adjustment is asked about, benign ones included. Only asking about the
    ones already known to be opaque would be scoring the model on an answer key
    it was handed.
    """
    out: list[Finding] = []
    subjects = [e for e in ix.sources.entries
                if e.entry_type is EntryType.ADJUSTMENT and not e.order_id]

    for e in sorted(subjects, key=lambda x: x.entry_id):
        answer = llm.ask(
            prompts.adjustment_prompt(e.narration, e.net, e.settlement_id),
            prompts.ADJUSTMENT_SCHEMA,
        )
        if answer is None:
            continue                     # no model, no claim
        try:
            explains = bool(answer["explains_purpose"])
            conf = float(answer.get("confidence", 0.0))
            evidence = str(answer.get("evidence", ""))[:200]
            category = str(answer.get("category", ""))[:80]
        except (KeyError, TypeError, ValueError):
            continue                     # malformed is unanswered

        if explains or conf < config.T3_MIN_CONFIDENCE:
            continue

        code = EC.UNEXPLAINED_ADJUSTMENT
        out.append(Finding(
            subject_type="entry", subject_id=e.entry_id, code=code,
            # Resolvability is `none`, so this escalates no matter how sure the
            # model is. The model decides whether a human looks, never whether
            # the case closes.
            action=Action.ESCALATED, rule="narration_explains_nothing", tier=T,
            confidence=conf,
            detail=(f"narration {e.narration!r} names no bookable reason "
                    f"(model read it as {category!r}, cited {evidence!r})"),
            amount_delta=e.net,
        ))
        _ = EXCEPTION_META[code]
    return out


# ---------------------------------------------------------------------------
# Job B: ambiguous amount ties
# ---------------------------------------------------------------------------

def r17_suggest_for_ambiguous(ix: Index, llm: LLMClient,
                              residue: list[Finding]) -> list[Finding]:
    """Offer a candidate where amount alone cannot choose.

    Emits a suggestion, never a link. If the model declines, the case stays
    exactly as open as T2 left it.
    """
    out: list[Finding] = []
    claimed_amounts: dict[int, list] = {}
    for st in ix.settlements.values():
        claimed_amounts.setdefault(st.payout, []).append(st)

    for f in residue:
        if f.rule != "amount_date_ambiguous" or f.subject_type != "settlement":
            continue
        st = ix.settlements.get(f.subject_id)
        if st is None:
            continue
        lines = [b for b in ix.credit_lines if b.credit == st.payout]
        if len(lines) < 2:
            continue
        b = lines[0]
        answer = llm.ask(
            prompts.tiebreak_prompt(
                b.line_id, b.credit, b.value_date, b.narration,
                [(s.settlement_id, s.utr) for s in claimed_amounts[st.payout]],
            ),
            prompts.TIEBREAK_SCHEMA,
        )
        if answer is None or not answer.get("can_decide"):
            continue
        conf = float(answer.get("confidence", 0.0))
        if conf < config.T3_MIN_CONFIDENCE:
            continue
        out.append(Finding(
            subject_type="settlement", subject_id=f.subject_id,
            action=Action.ESCALATED, rule="model_suggested_candidate", tier=T,
            confidence=conf,
            detail=(f"suggested match {answer.get('best_candidate_id')!r} for "
                    f"review, on reference text: "
                    f"{str(answer.get('evidence'))[:160]!r}. Not posted."),
            amount_delta=st.payout,
        ))
    return out


# ---------------------------------------------------------------------------
# Job C: the queue in plain English
# ---------------------------------------------------------------------------

def r18_explain(ix: Index, llm: LLMClient,
                findings: list[Finding]) -> dict[str, str]:
    """One note per exception class, plus the largest individual items.

    Grouped on purpose: the call count stays flat as the dataset grows instead
    of scaling with it -- roughly twenty calls whether the batch is 189 records
    or 10,441.
    """
    out: dict[str, str] = {}

    groups: dict[EC, list[Finding]] = {}
    for f in findings:
        if f.code is not None:
            groups.setdefault(f.code, []).append(f)

    for code, items in sorted(groups.items(), key=lambda kv: kv[0].value):
        meta = EXCEPTION_META[code]
        total = sum(abs(f.amount_delta or 0) for f in items)
        answer = llm.ask(
            prompts.group_explanation_prompt(
                meta.label, meta.description, items[0].action.value,
                len(items), total, items[0].detail,
            ),
            prompts.EXPLANATION_SCHEMA,
        )
        if answer is None:
            continue
        text = str(answer.get("explanation", "")).strip()
        step = str(answer.get("next_step", "")).strip()
        if text:
            out[f"code:{code.value}"] = f"{text} Next: {step}" if step else text

    open_items = sorted(
        (f for f in findings if f.action is Action.ESCALATED),
        key=lambda f: -abs(f.amount_delta or 0),
    )[:config.QUEUE_TOP_N]
    for f in open_items:
        label = EXCEPTION_META[f.code].label if f.code else "Unexplained break"
        answer = llm.ask(
            prompts.item_explanation_prompt(
                f.subject_key(), label, abs(f.amount_delta or 0), f.detail),
            prompts.EXPLANATION_SCHEMA,
        )
        if answer is None:
            continue
        text = str(answer.get("explanation", "")).strip()
        step = str(answer.get("next_step", "")).strip()
        if text:
            out[f.subject_key()] = f"{text} Next: {step}" if step else text
    return out


# ---------------------------------------------------------------------------

def run(ix: Index, llm: LLMClient,
        residue: list[Finding]) -> tuple[list[Finding], dict[str, str]]:
    """-> (findings, explanations). No links, by design."""
    findings = r16_classify_adjustments(ix, llm)
    findings += r17_suggest_for_ambiguous(ix, llm, residue)
    return findings, {}
