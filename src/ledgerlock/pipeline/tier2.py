"""Tier 2: rules, tolerances and bounded search.

T1 refuses every threshold, which is what makes it trustworthy and also what
leaves it unable to say whether a gap is absorbable rounding or a real loss.
T2 is where thresholds are allowed -- and every one of them is named, lives in
`config`, and is reported alongside the decision it drove.

Two things T2 is deliberately not allowed to do:

  * Auto-resolve a code whose resolvability is not FULL. E05 and E12 get
    correctly *classified* here and still go to a human. Classification and
    resolution are different verbs, and merging them is how a reconciler
    starts closing cases it has no business closing.
  * Guess between two answers. Both search rules (R11, R12) require a unique
    candidate and escalate when they find more than one.
"""

from __future__ import annotations

from datetime import date

from .. import config
from ..domain.models import EntryType
from ..domain.money import fmt
from ..domain.taxonomy import EXCEPTION_META, ExceptionCode as EC, Resolvability
from . import subsetsum
from .result import Action, Finding, ProposedLink, Tier
from .views import Index, SettlementView

T = Tier.T2

# Evidence strengths. Exact arithmetic with a unique candidate is stronger than
# a tolerance-band absorption, and both sit above the auto-resolve threshold --
# so the resolvability guard, not the number, is what holds E05 and E12 back.
CONF_EXACT_UNIQUE = 0.95
CONF_WITHIN_TOLERANCE = 0.90
CONF_AMBIGUOUS = 0.50


def _action_for(code: EC, confidence: float) -> Action:
    """The one place an action is decided.

    A rule says how good its evidence is; this decides what may be done about
    it. Resolvability wins over confidence every time.
    """
    if EXCEPTION_META[code].resolvability is not Resolvability.FULL:
        return Action.ESCALATED
    if confidence >= config.AUTO_RESOLVE_MIN_CONFIDENCE:
        return Action.AUTO_RESOLVED
    return Action.ESCALATED


def _days(a: date | None, b: date | None) -> int:
    if a is None or b is None:
        return 10**6
    return abs((a - b).days)


# ---------------------------------------------------------------------------
# classification: naming what T1 could only measure
# ---------------------------------------------------------------------------

def r9_classify_fee_drift(residue: list[Finding]) -> list[Finding]:
    """A recomputed fee that disagrees with the stated one: rounding or loss?

    Inside the tolerance it is absorbed -- but it is still reported, with the
    amount. A finance team wants to know that Rs 3.95 of fee drift was absorbed
    this cycle even though nobody needs to act on it. Silently swallowing it is
    how small systematic overcharges live forever.
    """
    out: list[Finding] = []
    tol = config.ROUNDING_TOLERANCE_PAISE
    for f in residue:
        if f.rule != "fee_recompute":
            continue
        delta = f.amount_delta or 0
        if abs(delta) <= tol:
            code, conf = EC.ROUNDING_DRIFT, CONF_WITHIN_TOLERANCE
            detail = (f"fee differs by {fmt(delta)}, inside the {fmt(tol)} "
                      "tolerance; absorbed and reported, not dropped")
        else:
            code, conf = EC.MATERIAL_MISMATCH, CONF_EXACT_UNIQUE
            detail = (f"fee differs by {fmt(delta)}, beyond the {fmt(tol)} "
                      "tolerance; no safe automatic answer")
        out.append(Finding(
            subject_type=f.subject_type, subject_id=f.subject_id, code=code,
            action=_action_for(code, conf), rule="fee_delta_vs_tolerance",
            tier=T, confidence=conf, detail=detail, amount_delta=delta,
        ))
    return out


def r10_classify_gross_mismatch(residue: list[Finding]) -> list[Finding]:
    """Gateway gross against the merchant's own order value.

    Never absorbed, at any size. A fee is something we recompute, so a small
    disagreement is arithmetic. A gross is something the merchant asserted, so
    any disagreement is two systems telling different stories about what was
    sold -- and that is a question for a person even when it is small.
    """
    out: list[Finding] = []
    tol = config.ROUNDING_TOLERANCE_PAISE
    for f in residue:
        if f.rule != "gross_vs_order_amount":
            continue
        delta = f.amount_delta or 0
        material = abs(delta) > tol
        code = EC.MATERIAL_MISMATCH if material else EC.ROUNDING_DRIFT
        out.append(Finding(
            subject_type=f.subject_type, subject_id=f.subject_id, code=code,
            action=Action.ESCALATED,          # never auto-resolved, by policy
            rule="gross_delta_vs_order", tier=T, confidence=CONF_EXACT_UNIQUE,
            detail=(f"gateway gross differs from the ERP order value by "
                    f"{fmt(delta)}; order-value gaps are never absorbed"),
            amount_delta=delta,
        ))
    return out


def r14_cross_cycle_refund(ix: Index) -> list[Finding]:
    """A refund settled in a later cycle than the payment it reverses.

    Sits in T2 rather than T1 because it is not a fact about one record: it
    pairs two records across cycle boundaries to explain why *this* payout is
    lower than its own captures suggest. That explanation is batch-level
    reasoning, which is what T2 is for.
    """
    payments: dict[str, object] = {}
    for e in ix.sources.entries:
        if e.entry_type is EntryType.PAYMENT and e.payment_id:
            payments[e.payment_id] = e
    by_order = {e.order_id: e for e in ix.sources.entries
                if e.entry_type is EntryType.PAYMENT and e.order_id}

    out: list[Finding] = []
    for r in ix.sources.entries:
        if r.entry_type is not EntryType.REFUND or r.settled_at is None:
            continue
        p = payments.get(r.payment_id or "") or by_order.get(r.order_id or "")
        if p is None or p.settled_at is None or p.settled_at >= r.settled_at:
            continue
        code = EC.CROSS_CYCLE_REFUND
        out.append(Finding(
            subject_type="entry", subject_id=r.entry_id, code=code,
            action=Action.DEFERRED, rule="refund_settled_after_its_payment",
            tier=T, confidence=CONF_EXACT_UNIQUE,
            detail=(f"reverses {p.entry_id}, which settled {p.settled_at} in an "
                    f"earlier cycle; drags the {r.settled_at} payout by "
                    f"{fmt(abs(r.net))}"),
            amount_delta=r.net,
        ))
    return out


# ---------------------------------------------------------------------------
# recovery: links T1 could not make
# ---------------------------------------------------------------------------

def _linked_lines(links: list[ProposedLink]) -> dict[str, set[str]]:
    """line_id -> settlement_ids already linked to it."""
    out: dict[str, set[str]] = {}
    for l in links:
        if l.link_type == "settlement_bank" and l.line_id:
            out.setdefault(l.line_id, set()).add(l.settlement_id or "")
    return out


def _unmatched_settlements(ix: Index, links: list[ProposedLink]) -> list[SettlementView]:
    linked = {l.settlement_id for l in links if l.link_type == "settlement_bank"}
    return sorted((st for sid, st in ix.settlements.items() if sid not in linked),
                  key=lambda s: (s.value_date or date.min, s.settlement_id))


def r11_recover_by_amount_and_date(
    ix: Index, links: list[ProposedLink]
) -> tuple[list[ProposedLink], list[Finding]]:
    """Recover a payout whose UTR the bank mangled, using amount and date.

    The taxonomy predicted this case needed a model, on the assumption that a
    corrupted UTR is a string problem. It is not. `HDFCN26O6O3OOOOI` is a bad
    string to match, but the payout is an exact integer and the value date is
    inside a known window -- and exact amount equality is far stronger evidence
    than any edit-distance guess. So this is deterministic after all, and T3
    never sees it.

    Requires a *unique* candidate. Two payouts of the same size in the same
    window cannot be told apart by amount, and that ambiguity is escalated
    rather than resolved by coin flip.
    """
    new_links: list[ProposedLink] = []
    findings: list[Finding] = []
    claimed = set(_linked_lines(links))
    available = [b for b in ix.credit_lines if b.line_id not in claimed]

    for st in _unmatched_settlements(ix, links):
        hits = [b for b in available
                if b.credit == st.payout
                and _days(b.value_date, st.value_date) <= config.DATE_WINDOW_DAYS]
        if not hits:
            continue
        if len(hits) > 1:
            findings.append(Finding(
                subject_type="settlement", subject_id=st.settlement_id,
                action=Action.ESCALATED, rule="amount_date_ambiguous", tier=T,
                confidence=CONF_AMBIGUOUS,
                detail=(f"{len(hits)} bank credits match this payout of "
                        f"{fmt(st.payout)} inside a {config.DATE_WINDOW_DAYS}-day "
                        "window; amount alone cannot choose"),
                amount_delta=st.payout,
            ))
            continue

        b = hits[0]
        available.remove(b)
        new_links.append(ProposedLink(
            link_type="settlement_bank", settlement_id=st.settlement_id,
            line_id=b.line_id, rule="amount_date_unique", tier=T,
            confidence=CONF_EXACT_UNIQUE,
            evidence=(f"credit {fmt(b.credit)} on {b.value_date} is the only "
                      f"unclaimed credit equal to this payout within "
                      f"{config.DATE_WINDOW_DAYS} days; stated UTR "
                      f"{st.utr!r} does not appear in the narration"),
        ))
        code = EC.UTR_CORRUPTED
        findings.append(Finding(
            subject_type="bank_line", subject_id=b.line_id, code=code,
            action=_action_for(code, CONF_EXACT_UNIQUE),
            rule="amount_date_unique", tier=T, confidence=CONF_EXACT_UNIQUE,
            detail=(f"narration does not carry UTR {st.utr}; matched to "
                    f"{st.settlement_id} on exact amount and date"),
            supersedes=(f"settlement:{st.settlement_id}",),
        ))
    return new_links, findings


def r12_subset_sum_merged_credits(
    ix: Index, links: list[ProposedLink]
) -> tuple[list[ProposedLink], list[Finding]]:
    """One bank credit covering several payouts.

    The line is already linked by UTR to one settlement but its credit is
    larger, so the excess should be exactly the payout of one or more
    settlements that found no line of their own. Bounded, exact, and it refuses
    to choose between two subsets that both work.
    """
    new_links: list[ProposedLink] = []
    findings: list[Finding] = []
    linked = _linked_lines(links)
    unmatched = _unmatched_settlements(ix, links)
    if not unmatched:
        return new_links, findings

    for line_id, sids in sorted(linked.items()):
        b = ix.bank.get(line_id)
        if b is None or b.credit <= 0:
            continue
        accounted = sum(ix.settlements[s].payout for s in sids if s in ix.settlements)
        gap = b.credit - accounted
        if gap <= 0:
            continue

        pool = [(st.settlement_id, st.payout) for st in unmatched
                if _days(b.value_date, st.value_date) <= config.DATE_WINDOW_DAYS]
        res = subsetsum.find_unique_subset(
            gap, pool,
            max_subset=config.SUBSET_SUM_MAX_SUBSET,
            max_candidates=config.SUBSET_SUM_MAX_CANDIDATES,
        )
        if not res.found:
            findings.append(Finding(
                subject_type="bank_line", subject_id=line_id,
                action=Action.ESCALATED, rule="subset_sum_no_unique_answer",
                tier=T, confidence=CONF_AMBIGUOUS,
                detail=(f"credit exceeds {', '.join(sorted(sids))} by "
                        f"{fmt(gap)}; {res.why_not()}"),
                amount_delta=gap,
            ))
            continue

        recovered = list(res.subset or ())
        for sid in recovered:
            new_links.append(ProposedLink(
                link_type="settlement_bank", settlement_id=sid, line_id=line_id,
                rule="subset_sum_exact", tier=T, confidence=CONF_EXACT_UNIQUE,
                evidence=(f"credit {fmt(b.credit)} equals "
                          f"{', '.join(sorted(sids))} plus {sid}; the only "
                          f"subset of {res.considered} candidates that sums to "
                          f"the {fmt(gap)} excess"),
            ))
        code = EC.MERGED_SETTLEMENT
        findings.append(Finding(
            subject_type="bank_line", subject_id=line_id, code=code,
            action=_action_for(code, CONF_EXACT_UNIQUE), rule="subset_sum_exact",
            tier=T, confidence=CONF_EXACT_UNIQUE,
            detail=(f"one credit covers {len(sids) + len(recovered)} payouts: "
                    f"{', '.join(sorted(set(sids) | set(recovered)))}"),
            amount_delta=gap,
            supersedes=tuple(f"settlement:{s}" for s in recovered),
        ))
        unmatched = [st for st in unmatched if st.settlement_id not in recovered]
    return new_links, findings


def r13_classify_non_gateway_inflow(
    ix: Index, links: list[ProposedLink], residue: list[Finding]
) -> list[Finding]:
    """A credit that was never gateway money.

    Runs last of the bank-side rules, so it only ever sees what the recovery
    rules could not claim. The correct outcome is "out of scope", not
    "unmatched" -- a reconciler that reports the merchant's other income as a
    break is padding its own exception list.

    A credit remitted by the gateway but still unexplained is *not* classified
    here. That stays open, because being unable to explain our own money is a
    real problem and hiding it under this code would be the tidy lie.
    """
    claimed = set(_linked_lines(links))
    payouts = {st.payout for st in ix.settlements.values()}
    remitter = config.GATEWAY_REMITTER.upper()

    out: list[Finding] = []
    for f in residue:
        if f.rule != "unclaimed_credit" or f.subject_type != "bank_line":
            continue
        b = ix.bank.get(f.subject_id)
        if b is None or b.line_id in claimed:
            continue
        narration = (b.narration or "").upper()
        if remitter in narration or b.credit in payouts:
            continue                      # plausibly ours; leave it open
        code = EC.NON_PG_INFLOW
        out.append(Finding(
            subject_type="bank_line", subject_id=b.line_id, code=code,
            action=Action.OUT_OF_SCOPE, rule="not_remitted_by_gateway", tier=T,
            confidence=CONF_EXACT_UNIQUE,
            detail=(f"credit of {fmt(b.credit)} is not remitted by "
                    f"{config.GATEWAY_REMITTER} and matches no payout amount"),
            amount_delta=b.credit,
        ))
    return out


# ---------------------------------------------------------------------------
# attribution: turning a break into an explanation
# ---------------------------------------------------------------------------

def r15_explain_batch_gap(ix: Index, classified: list[Finding]) -> list[Finding]:
    """Account for a settlement that does not tie internally.

    T1 can only say "members do not add up to the payout". Once R9 and R10 have
    sized each member's break, the gap can be attributed: a batch off by
    Rs 0.42 because of three fee drifts is reconciled, not broken.
    """
    delta_by_entry = {f.subject_id: (f.amount_delta or 0) for f in classified
                      if f.subject_type == "entry"
                      and f.rule in ("fee_delta_vs_tolerance", "gross_delta_vs_order")}
    out: list[Finding] = []
    for sid, st in sorted(ix.settlements.items()):
        gap = st.payout - st.member_net
        if gap == 0:
            continue
        explained = sum(delta_by_entry.get(e.entry_id, 0) for e in st.members)
        if explained != gap:
            continue
        out.append(Finding(
            subject_type="settlement", subject_id=sid, action=Action.EXPLAINED,
            rule="batch_gap_attributed", tier=T, confidence=CONF_EXACT_UNIQUE,
            detail=(f"members are {fmt(-gap)} off the stated payout, fully "
                    f"accounted for by member breaks already reported"),
            amount_delta=gap,
        ))
    return out


# ---------------------------------------------------------------------------

def run(ix: Index, links: list[ProposedLink],
        residue: list[Finding]) -> tuple[list[ProposedLink], list[Finding]]:
    """Run T2 over T1's output. `residue` is what T1 flagged but could not name."""
    new_links: list[ProposedLink] = []
    findings: list[Finding] = []

    classified = r9_classify_fee_drift(residue) + r10_classify_gross_mismatch(residue)
    findings += classified
    findings += r14_cross_cycle_refund(ix)

    # Recovery order matters: exact single-amount matches first, then the
    # subset search, then out-of-scope over whatever is genuinely left.
    l, f = r11_recover_by_amount_and_date(ix, links)
    new_links += l
    findings += f

    l, f = r12_subset_sum_merged_credits(ix, links + new_links)
    new_links += l
    findings += f

    findings += r13_classify_non_gateway_inflow(ix, links + new_links, residue)
    findings += r15_explain_batch_gap(ix, classified)
    return new_links, findings
