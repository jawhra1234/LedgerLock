"""Exception injectors.

Two rules keep the ground truth interpretable:

  * One injector corrupts exactly one dimension. E04 breaks an amount and
    nothing else; E10 breaks a link and nothing else. If a single record were
    hit by two injectors, no evaluator could say which failure the pipeline was
    supposed to find -- hence World.claim().

  * Injectors that represent *real money moving* keep the gateway ledger and
    the bank statement in agreement (they bump the bank credit too). Injectors
    that represent a *reporting error* deliberately leave them disagreeing.
    That distinction is what lets the pipeline tell a business exception from
    an arithmetic break.
"""

from __future__ import annotations

import random
from datetime import timedelta

from ..domain.models import BankLine, EntryType, PGEntry, TruthLink
from ..domain.money import Paise, rupees
from ..domain.taxonomy import ExceptionCode as EC
from .. import config
from . import fees
from .engine import World, _stamp, business_day

OPAQUE_NARRATIONS = [
    "MISC DR REF 88213",
    "ADJ-BATCH-CORR-Q2",
    "MANUAL ENTRY 4471 / NO REF",
    "RECON DIFF WRITEOFF",
    "PLATFORM ADJ 0091",
]


def _pick(w: World, pool, n: int, key=lambda x: x):
    """Yield up to `n` uncontested candidates, claiming each as it is yielded.

    Shuffle-and-skip rather than sample-and-give-up: if another injector
    already owns a candidate we keep looking, so a code never silently
    under-injects and leaves the pipeline untested on that branch.
    """
    items = list(pool)
    w.rng.shuffle(items)
    taken = 0
    for item in items:
        if taken >= n:
            return
        k = key(item)
        keys = k if isinstance(k, tuple) else (k,)
        if any(w.is_claimed(x) for x in keys):
            continue
        for x in keys:
            w.claim(x)
        taken += 1
        yield item


def _bump_bank(w: World, settlement_id: str, delta: Paise) -> None:
    """Keep the bank credit in step with a ledger change (real money moved).

    All three views of the payout have to move together: the bank credit, the
    internal Settlement, and the gateway's own `settlement` summary row. Miss
    the third and the gateway ledger contradicts itself, which would look to
    the pipeline like an exception we never injected.
    """
    st = w.settlements[settlement_id]
    line = w.bank_line(st.line_id or "")
    if line is None:
        return
    line.credit += delta
    st.payout += delta
    row = w.settlement_row(settlement_id)
    if row is not None:
        row.net -= delta          # the summary row is the mirror of the payout


def _settled_payments(w: World) -> list[PGEntry]:
    return [e for e in w.entries
            if e.entry_type is EntryType.PAYMENT and e.settlement_id]


# ---------------------------------------------------------------------------
# amount-consistent structural exceptions (bank and ledger still agree)
# ---------------------------------------------------------------------------

def e01_duplicate_payment(w: World, n: int) -> None:
    """The customer really was charged twice. The money is in the bank, so the
    question is a business one -- refund it or keep it -- and only a human can
    answer. Hence resolvability PARTIAL."""
    for src in _pick(w, _settled_payments(w), n, lambda e: e.entry_id):
        dup = src.model_copy(update={
            "entry_id": w.nid("ENT"),
            "payment_id": w.nid("PAY"),
            "created_at": src.created_at + timedelta(seconds=w.rng.randint(18, 240)),
        })
        w.entries.append(dup)
        w.claim(dup.entry_id)
        w.links.append(TruthLink(link_type="order_entry",
                                 order_id=dup.order_id, entry_id=dup.entry_id))
        w.links.append(TruthLink(link_type="entry_settlement",
                                 entry_id=dup.entry_id,
                                 settlement_id=dup.settlement_id))
        _bump_bank(w, dup.settlement_id or "", dup.net)
        w.flag("entry", dup.entry_id, EC.DUPLICATE_PAYMENT,
               f"second capture on {dup.order_id}, original {src.entry_id}")


def e02_missing_in_pg(w: World, n: int) -> None:
    """The ERP believes the order was paid; the gateway has no record. The
    gateway and bank still agree with each other -- only the books are wrong."""
    order_counts: dict[str, int] = {}
    for e in w.entries:
        if e.order_id:
            order_counts[e.order_id] = order_counts.get(e.order_id, 0) + 1
    # Only touch orders with a single gateway entry, so deleting it cannot
    # orphan a refund or chargeback and blur the exception.
    pool = [e for e in _settled_payments(w) if order_counts.get(e.order_id or "") == 1]
    for e in _pick(w, pool, n, lambda x: (x.entry_id, x.order_id or "")):
        _bump_bank(w, e.settlement_id or "", -e.net)
        w.entries.remove(e)
        w.drop_links(entry_id=e.entry_id)
        w.flag("order", e.order_id or "", EC.MISSING_IN_PG,
               "ERP status paid, no gateway entry exists")


def e03_orphan_pg_entry(w: World, n: int) -> None:
    """A gateway entry pointing at an order the ERP has never heard of --
    typically a test transaction or an order deleted after capture."""
    settled = [s for s in w.settlements.values() if s.line_id]
    if not settled:
        return
    for _ in range(n):
        st = w.rng.choice(settled)
        ghost = f"ORD_TEST_{w.rng.randrange(10**5):05d}"
        method = w.rng.choice(list(config.MDR_RATES))
        gross = rupees(w.rng.randint(100, 5_000))
        fee, tax, net = fees.payment_net(method, gross)
        e = PGEntry(
            entry_id=w.nid("ENT"), entry_type=EntryType.PAYMENT,
            order_id=ghost, payment_id=w.nid("PAY"), method=method,
            gross=gross, fee=fee, tax=tax, net=net,
            settlement_id=st.settlement_id, settled_at=st.value_date,
            created_at=_stamp(w.rng, st.value_date - timedelta(days=2)),
        )
        w.entries.append(e)
        w.claim(e.entry_id)
        w.links.append(TruthLink(link_type="entry_settlement",
                                 entry_id=e.entry_id,
                                 settlement_id=st.settlement_id))
        _bump_bank(w, st.settlement_id, net)
        w.flag("entry", e.entry_id, EC.ORPHAN_PG_ENTRY,
               f"references {ghost}, absent from ERP")


def e12_unexplained_adjustment(w: World, n: int) -> None:
    """Real money, no linkage, opaque narration. A model can guess at its
    nature; nothing can legitimately match it. This is the honest-failure
    anchor of the dataset."""
    settled = [s for s in w.settlements.values() if s.line_id]
    if not settled:
        return
    for _ in range(n):
        st = w.rng.choice(settled)
        amount = rupees(w.rng.randint(500, 25_000))
        signed = amount if w.rng.random() < 0.4 else -amount
        e = PGEntry(
            entry_id=w.nid("ENT"), entry_type=EntryType.ADJUSTMENT,
            gross=amount, net=signed,
            settlement_id=st.settlement_id, settled_at=st.value_date,
            created_at=_stamp(w.rng, st.value_date),
            narration=w.rng.choice(OPAQUE_NARRATIONS),
        )
        w.entries.append(e)
        w.claim(e.entry_id)
        w.links.append(TruthLink(link_type="entry_settlement",
                                 entry_id=e.entry_id,
                                 settlement_id=st.settlement_id))
        _bump_bank(w, st.settlement_id, signed)
        w.flag("entry", e.entry_id, EC.UNEXPLAINED_ADJUSTMENT,
               "no order linkage, narration carries no usable reference")


# ---------------------------------------------------------------------------
# amount breaks (bank and ledger deliberately disagree)
# ---------------------------------------------------------------------------

def e04_rounding_drift(w: World, n: int) -> None:
    """Fee off by a few paise. Absorbable -- but the pipeline must report it as
    absorbed, with the amount, not silently swallow it."""
    pool = [e for e in _settled_payments(w) if e.fee > 100]
    for e in _pick(w, pool, n, lambda x: x.entry_id):
        delta = w.rng.choice([-1, 1]) * w.rng.randint(1, 50)
        e.fee += delta
        e.net = e.gross - e.fee - e.tax
        w.flag("entry", e.entry_id, EC.ROUNDING_DRIFT,
               f"stated fee differs from recomputed fee by {delta} paise")


def e05_material_mismatch(w: World, n: int) -> None:
    """Gateway gross disagrees with the order value by a real amount. There is
    no safe automatic answer, so the correct outcome is an escalation."""
    pool = [e for e in _settled_payments(w) if e.order_id]
    for e in _pick(w, pool, n, lambda x: (x.entry_id, x.order_id or "")):
        delta = rupees(w.rng.randint(100, 5_000)) * w.rng.choice([-1, 1])
        if e.gross + delta <= 0:
            delta = abs(delta)
        e.gross += delta
        e.fee, e.tax, e.net = fees.payment_net(e.method or "upi", e.gross)
        w.flag("entry", e.entry_id, EC.MATERIAL_MISMATCH,
               f"gateway gross differs from order amount by {delta} paise")


# ---------------------------------------------------------------------------
# label-only: states the clean world already produces, marked as expected
# ---------------------------------------------------------------------------

def e06_timing_unsettled(w: World, n: int) -> None:
    """Captured, cycle has not run yet. Correct handling is to defer, not to
    chase -- so a pipeline that reports these as breaks is crying wolf."""
    pool = [e for e in w.entries
            if e.entry_type is EntryType.PAYMENT and not e.settlement_id]
    for e in _pick(w, pool, n, lambda x: x.entry_id):
        w.flag("entry", e.entry_id, EC.TIMING_UNSETTLED,
               "captured after the settlement cut-off for this statement")


def e07_cross_cycle_refund(w: World, n: int) -> None:
    """A refund settled in a later cycle than the payment it reverses. It drags
    this cycle's payout down for a sale that was banked weeks ago."""
    pay_by_order: dict[str, PGEntry] = {
        e.order_id: e for e in w.entries
        if e.entry_type is EntryType.PAYMENT and e.order_id
    }
    pool = []
    for r in w.entries:
        if r.entry_type is not EntryType.REFUND or not r.settled_at:
            continue
        p = pay_by_order.get(r.order_id or "")
        if p is not None and p.settled_at and p.settled_at < r.settled_at:
            pool.append(r)
    for r in _pick(w, pool, n, lambda x: x.entry_id):
        p = pay_by_order[r.order_id or ""]
        w.flag("entry", r.entry_id, EC.CROSS_CYCLE_REFUND,
               f"reverses {p.entry_id} settled {p.settled_at} in an earlier cycle")


# ---------------------------------------------------------------------------
# bank-side link corruption (amounts still tie; only the joins are broken)
# ---------------------------------------------------------------------------

def _mangle_utr(rng: random.Random, utr: str) -> str:
    style = rng.randrange(4)
    if style == 0:
        return utr[:-3]                                  # truncated by the bank
    if style == 1:
        return utr.replace("0", "O").replace("1", "I")   # OCR-style confusion
    if style == 2:
        return f"{utr[:8]} {utr[8:]}"                    # stray space
    return f"REF:{utr[2:]}"                              # junk prefix, head lost


def e08_utr_corrupted(w: World, n: int) -> None:
    """The UTR is present but not machine-readable. Amounts tie, so this is
    purely a join problem -- the natural place for a fuzzy/LLM tier."""
    pool = [s for s in w.settlements.values() if s.line_id]
    for st in _pick(w, pool, n, lambda s: s.line_id or ""):
        line = w.bank_line(st.line_id or "")
        if line is None:
            continue
        bad = _mangle_utr(w.rng, st.utr)
        line.narration = line.narration.replace(st.utr, bad)
        line.utr = ""      # the bank's own parser gave up
        w.flag("bank_line", line.line_id, EC.UTR_CORRUPTED,
               f"true UTR {st.utr} appears as {bad!r}")


def e10_merged_settlement(w: World, n: int) -> None:
    """The bank aggregated two settlements into one credit. Only subset-sum
    over candidate settlements recovers the pair."""
    ordered = sorted((s for s in w.settlements.values() if s.line_id),
                     key=lambda s: (s.value_date, s.settlement_id))
    # Consecutive settlements within a few days of each other: the bank posted
    # both on the later day as one consolidated credit.
    pairs = [(a, b) for a, b in zip(ordered, ordered[1:])
             if (b.value_date - a.value_date).days <= 3]
    w.rng.shuffle(pairs)
    done = 0
    for a, b in pairs:
        if done >= n:
            break
        lines = [w.bank_line(a.line_id or ""), w.bank_line(b.line_id or "")]
        if any(l is None for l in lines):
            continue
        ids = {l.line_id for l in lines}
        # Skip a candidate another injector already owns and keep looking,
        # rather than silently under-injecting this code.
        if len(ids) < 2 or any(w.is_claimed(i) for i in ids):
            continue
        for i in ids:
            w.claim(i)
        done += 1
        total = sum(l.credit for l in lines)
        value_date = max(l.value_date for l in lines)
        for l in lines:
            w.bank_lines.remove(l)
        merged = BankLine(
            line_id=w.nid("LINE"), value_date=value_date,
            narration=(f"NEFT CR-HDFC0000123-RAZORPAY SOFTWARE PVT LTD"
                       f"-{a.utr}-CONSOLIDATED"),
            utr=a.utr, credit=total,
        )
        w.bank_lines.append(merged)
        w.claim(merged.line_id)
        for st in (a, b):
            w.drop_links(link_type="settlement_bank", settlement_id=st.settlement_id)
            w.links.append(TruthLink(link_type="settlement_bank",
                                     settlement_id=st.settlement_id,
                                     line_id=merged.line_id))
            st.line_id = merged.line_id
        w.flag("bank_line", merged.line_id, EC.MERGED_SETTLEMENT,
               f"one credit covers {a.settlement_id} and {b.settlement_id}")


def e11_split_settlement(w: World, n: int) -> None:
    """One settlement arrived as two credits. The mirror image of E10."""
    pool = [s for s in w.settlements.values() if s.line_id]
    for st in _pick(w, pool, n, lambda s: s.line_id or ""):
        line = w.bank_line(st.line_id or "")
        if line is None:
            continue
        first = line.credit // 2 + w.rng.randint(1, max(1, line.credit // 10))
        first = min(first, line.credit - 1)
        parts = [first, line.credit - first]
        w.bank_lines.remove(line)
        w.drop_links(link_type="settlement_bank", settlement_id=st.settlement_id)
        new_ids = []
        for i, amt in enumerate(parts, start=1):
            nl = BankLine(
                line_id=w.nid("LINE"),
                value_date=line.value_date + timedelta(days=i - 1),
                narration=(f"NEFT CR-HDFC0000123-RAZORPAY SOFTWARE PVT LTD"
                           f"-{st.utr}-SETTLEMENT PART {i}/2"),
                utr=st.utr, credit=amt,
            )
            w.bank_lines.append(nl)
            w.claim(nl.line_id)
            new_ids.append(nl.line_id)
            w.links.append(TruthLink(link_type="settlement_bank",
                                     settlement_id=st.settlement_id,
                                     line_id=nl.line_id))
        st.line_id = new_ids[0]
        w.flag("settlement", st.settlement_id, EC.SPLIT_SETTLEMENT,
               f"payout arrived as {len(parts)} credits: {new_ids}")


def e09_non_pg_inflow(w: World, n: int) -> None:
    """A genuine bank credit that is simply not gateway money. The correct
    outcome is 'out of scope', not 'unmatched' -- a matcher that reports these
    as breaks inflates its own exception list."""
    names = ["ACME RETAIL LLP", "K R ENTERPRISES", "ZENITH TRADING CO",
             "S KUMAR", "NORTHSTAR LABS PVT LTD"]
    for _ in range(n):
        d = business_day(config.WORLD_START
                         + timedelta(days=w.rng.randrange(w.spec.n_days)))
        payer = w.rng.choice(names)
        line = BankLine(
            line_id=w.nid("LINE"), value_date=d,
            narration=(f"NEFT CR-ICIC0004567-{payer}-INV SETTLEMENT-"
                       f"ICICN{d.strftime('%y%m%d')}{w.rng.randrange(10**5):05d}"),
            credit=rupees(w.rng.randint(5_000, 400_000)),
        )
        w.bank_lines.append(line)
        w.claim(line.line_id)
        w.flag("bank_line", line.line_id, EC.NON_PG_INFLOW,
               f"direct customer transfer from {payer}, not a gateway payout")


# ---------------------------------------------------------------------------

# Ordered deliberately: money-consistent structural changes first (they move
# bank credits around), then amount breaks, then labels, then link corruption,
# then extra lines. Reordering would let a later injector overwrite an earlier
# one's evidence.
PIPELINE = [
    (EC.DUPLICATE_PAYMENT, e01_duplicate_payment),
    (EC.MISSING_IN_PG, e02_missing_in_pg),
    (EC.ORPHAN_PG_ENTRY, e03_orphan_pg_entry),
    (EC.UNEXPLAINED_ADJUSTMENT, e12_unexplained_adjustment),
    (EC.ROUNDING_DRIFT, e04_rounding_drift),
    (EC.MATERIAL_MISMATCH, e05_material_mismatch),
    (EC.TIMING_UNSETTLED, e06_timing_unsettled),
    (EC.CROSS_CYCLE_REFUND, e07_cross_cycle_refund),
    (EC.UTR_CORRUPTED, e08_utr_corrupted),
    (EC.MERGED_SETTLEMENT, e10_merged_settlement),
    (EC.SPLIT_SETTLEMENT, e11_split_settlement),
    (EC.NON_PG_INFLOW, e09_non_pg_inflow),
]


def inject_all(w: World) -> None:
    for code, fn in PIPELINE:
        fn(w, w.spec.count_for(code))
