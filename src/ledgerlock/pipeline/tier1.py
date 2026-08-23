"""Tier 1: deterministic.

The rule for what belongs in this tier is strict and worth stating, because it
is the whole basis of the tiered design: **T1 uses no thresholds.** Existence
checks, exact key equality, exact arithmetic. Nothing here has a tolerance, a
date window, a fuzzy score or a model call. If a rule needs to decide "how
close is close enough", it is not a T1 rule.

That strictness is what makes T1's output trustworthy enough to post without
review, and it is why T1 is allowed to run before anything else.
"""

from __future__ import annotations

from ..domain.models import EntryType, OrderStatus
from ..domain.taxonomy import ExceptionCode as EC
from ..domain import fees
from .result import Action, Finding, ProposedLink, Tier
from .views import Index

T = Tier.T1


def r1_order_entry_exact(ix: Index) -> tuple[list[ProposedLink], list[Finding]]:
    """Confirm every claimed order reference actually resolves.

    The gateway report *claims* an order_id. Trusting that column is how orphan
    test transactions get silently booked as revenue, so the claim is verified
    rather than accepted.
    """
    links: list[ProposedLink] = []
    findings: list[Finding] = []
    for e in ix.sources.entries:
        if not e.order_id:
            continue
        if e.order_id in ix.orders:
            links.append(ProposedLink(
                link_type="order_entry", order_id=e.order_id, entry_id=e.entry_id,
                rule="order_id_exact", tier=T,
                evidence=f"gateway entry cites {e.order_id}, which exists in the ERP",
            ))
        else:
            findings.append(Finding(
                subject_type="entry", subject_id=e.entry_id, code=EC.ORPHAN_PG_ENTRY,
                action=Action.ESCALATED, rule="order_id_exact", tier=T,
                detail=f"cites {e.order_id}, absent from the ERP",
            ))
    return links, findings


def r2_order_missing_in_gateway(ix: Index) -> list[Finding]:
    """An order the ERP believes was paid, with nothing in the gateway.

    Failed orders are skipped deliberately: an ERP order that never completed
    has no gateway entry and correctly so. Flagging those is the single easiest
    way to inflate an exception list with noise.
    """
    out: list[Finding] = []
    for o in ix.sources.orders:
        if o.status is OrderStatus.FAILED:
            continue
        if not ix.entries_by_order.get(o.order_id):
            out.append(Finding(
                subject_type="order", subject_id=o.order_id, code=EC.MISSING_IN_PG,
                action=Action.ESCALATED, rule="order_has_no_entry", tier=T,
                detail=f"ERP status {o.status.value}, no gateway entry references it",
                amount_delta=o.amount,
            ))
    return out


def r3_duplicate_capture(ix: Index) -> list[Finding]:
    """Two captures on one order for the identical amount.

    Exact, so it belongs in T1 -- but escalated, never auto-resolved. Whether
    the customer should be refunded is a business decision, and the pipeline
    has no standing to make it.
    """
    out: list[Finding] = []
    groups: dict[tuple[str, int], list] = {}
    for e in ix.sources.entries:
        if e.entry_type is EntryType.PAYMENT and e.order_id:
            groups.setdefault((e.order_id, e.gross), []).append(e)
    for (order_id, gross), group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda e: (e.created_at, e.entry_id))
        first = group[0]
        for dup in group[1:]:
            gap = (dup.created_at - first.created_at).total_seconds()
            out.append(Finding(
                subject_type="entry", subject_id=dup.entry_id,
                code=EC.DUPLICATE_PAYMENT, action=Action.ESCALATED,
                rule="repeat_capture_same_order_same_amount", tier=T,
                detail=(f"identical capture on {order_id} {gap:.0f}s after "
                        f"{first.entry_id}"),
                amount_delta=gross,
            ))
    return out


def r4_unsettled(ix: Index) -> list[Finding]:
    """Captured but not yet in a payout. Deferred, not escalated -- there is
    nothing wrong here and nobody should be paged about it."""
    return [
        Finding(
            subject_type="entry", subject_id=e.entry_id, code=EC.TIMING_UNSETTLED,
            action=Action.DEFERRED, rule="no_settlement_reference", tier=T,
            detail=f"{e.entry_type.value} captured {e.created_at.date()}, "
                   "settlement cycle has not run",
            amount_delta=e.net,
        )
        for e in ix.sources.entries
        if e.settlement_id is None
    ]


def r5_settlement_bank_exact_utr(ix: Index) -> tuple[list[ProposedLink], list[Finding]]:
    """Join gateway payouts to bank credits on an exact UTR occurrence.

    The UTR is the authoritative key, so an exact hit is a link even when the
    amount disagrees -- the amount disagreeing is then a *separate* finding
    about the bank line, not a reason to reject a certain join.
    """
    links: list[ProposedLink] = []
    findings: list[Finding] = []
    credits = ix.credit_lines

    for sid, st in sorted(ix.settlements.items()):
        if not st.utr:
            findings.append(Finding(
                subject_type="settlement", subject_id=sid, action=Action.ESCALATED,
                rule="utr_exact", tier=T,
                detail="gateway settlement row carries no parseable UTR",
            ))
            continue

        hits = [b for b in credits
                if b.utr == st.utr or st.utr in (b.narration or "")]
        if not hits:
            findings.append(Finding(
                subject_type="settlement", subject_id=sid, action=Action.ESCALATED,
                rule="utr_exact", tier=T,
                detail=f"no bank credit carries UTR {st.utr}",
                amount_delta=st.payout,
            ))
            continue

        for b in hits:
            links.append(ProposedLink(
                link_type="settlement_bank", settlement_id=sid, line_id=b.line_id,
                rule="utr_exact", tier=T,
                evidence=f"UTR {st.utr} appears verbatim in {b.line_id} narration",
            ))

        credited = sum(b.credit for b in hits)
        if len(hits) > 1 and credited == st.payout:
            # Several credits carrying one UTR and summing to the payout is a
            # split settlement, provable without any tolerance.
            findings.append(Finding(
                subject_type="settlement", subject_id=sid,
                code=EC.SPLIT_SETTLEMENT, action=Action.AUTO_RESOLVED,
                rule="utr_exact_multi_line_sum", tier=T,
                detail=(f"payout arrived as {len(hits)} credits "
                        f"({', '.join(b.line_id for b in hits)}) summing exactly"),
            ))
        elif credited != st.payout:
            # The join is certain, the amount is not. Attributing the gap needs
            # search over other settlements, which is T2's job -- so this is
            # reported as an unnamed break rather than guessed at.
            for b in hits:
                findings.append(Finding(
                    subject_type="bank_line", subject_id=b.line_id,
                    action=Action.ESCALATED, rule="utr_exact_amount_break", tier=T,
                    detail=(f"credit does not equal payout of {sid}; "
                            "cause not attributable at t1"),
                    amount_delta=credited - st.payout,
                ))
    return links, findings


def r6_unmatched_credits(ix: Index, links: list[ProposedLink]) -> list[Finding]:
    """Bank credits no gateway payout claims. Genuinely unresolved at T1."""
    claimed = {l.line_id for l in links if l.link_type == "settlement_bank"}
    return [
        Finding(
            subject_type="bank_line", subject_id=b.line_id, action=Action.ESCALATED,
            rule="unclaimed_credit", tier=T,
            detail="credit matches no gateway payout UTR",
            amount_delta=b.credit,
        )
        for b in ix.credit_lines if b.line_id not in claimed
    ]


def r7_fee_consistency(ix: Index) -> list[Finding]:
    """Recompute every fee from the slab and compare exactly.

    Whether a gap is absorbable rounding or a real overcharge is a question
    about magnitude, and magnitude means a threshold -- so T1 reports the delta
    and declines to name it.
    """
    out: list[Finding] = []
    for e in ix.sources.entries:
        if e.entry_type is not EntryType.PAYMENT or not e.method:
            continue
        expected_fee, expected_tax, _ = fees.payment_net(e.method, e.gross)
        if e.fee == expected_fee and e.tax == expected_tax:
            continue
        out.append(Finding(
            subject_type="entry", subject_id=e.entry_id, action=Action.ESCALATED,
            rule="fee_recompute", tier=T,
            detail=(f"stated fee {e.fee}+{e.tax} tax, recomputed "
                    f"{expected_fee}+{expected_tax} for {e.method} on {e.gross}"),
            amount_delta=(e.fee + e.tax) - (expected_fee + expected_tax),
        ))
    return out


def r8_gross_vs_order(ix: Index) -> list[Finding]:
    """Gateway gross against the ERP's own order value. Exact comparison."""
    out: list[Finding] = []
    for e in ix.sources.entries:
        if e.entry_type is not EntryType.PAYMENT or not e.order_id:
            continue
        order = ix.orders.get(e.order_id)
        if order is None or e.gross == order.amount:
            continue
        out.append(Finding(
            subject_type="entry", subject_id=e.entry_id, action=Action.ESCALATED,
            rule="gross_vs_order_amount", tier=T,
            detail=(f"gateway gross {e.gross} against ERP order amount "
                    f"{order.amount}"),
            amount_delta=e.gross - order.amount,
        ))
    return out


def run(ix: Index) -> tuple[list[ProposedLink], list[Finding]]:
    links: list[ProposedLink] = []
    findings: list[Finding] = []

    l, f = r1_order_entry_exact(ix)
    links += l
    findings += f

    findings += r2_order_missing_in_gateway(ix)
    findings += r3_duplicate_capture(ix)
    findings += r4_unsettled(ix)

    l, f = r5_settlement_bank_exact_utr(ix)
    links += l
    findings += f

    findings += r6_unmatched_credits(ix, links)
    findings += r7_fee_consistency(ix)
    findings += r8_gross_vs_order(ix)
    return links, findings
