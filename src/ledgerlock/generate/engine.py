"""The world generator.

Strategy, and the reason the accuracy numbers mean anything:

  1. Build a *perfectly reconciling* world. Every settlement satisfies the
     identity  sum(member entry nets) == payout == bank credit.
  2. Assert that identity. If step 1 has a bug, generation fails loudly here
     rather than silently producing exceptions we did not intend.
  3. Apply injectors, each of which corrupts exactly one dimension (an amount,
     a link, a narration) and records a ground-truth exception row.

So every exception in the dataset is there on purpose, and the pipeline's
accuracy is measured against a list we wrote down before we built the matcher.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .. import config
from ..domain.models import (
    BankLine, EntryType, Order, OrderStatus, PGEntry, TruthException, TruthLink,
)
from ..domain.money import Paise, rupees
from ..domain.taxonomy import EXCEPTION_META, ExceptionCode
from ..domain import fees
from .params import ScenarioSpec

BANK_CODE = "HDFC"
OPENING_BALANCE = rupees(500_000)

# Sale-value bands, roughly log-distributed the way real merchant traffic is.
AMOUNT_BANDS: list[tuple[int, int, float]] = [
    (99, 1_500, 0.45),
    (1_500, 8_000, 0.33),
    (8_000, 60_000, 0.18),
    (60_000, 250_000, 0.04),
]


@dataclass
class Settlement:
    """Internal only -- never written to disk. The gateway exposes this as a
    `settlement` ledger entry plus a UTR; the reconciler has to rebuild it."""
    settlement_id: str
    value_date: date
    utr: str
    member_entry_ids: list[str] = field(default_factory=list)
    gross_total: Paise = 0
    tds: Paise = 0
    reserve_held: Paise = 0
    payout: Paise = 0
    line_id: str | None = None


@dataclass
class World:
    spec: ScenarioSpec
    rng: random.Random
    orders: list[Order] = field(default_factory=list)
    entries: list[PGEntry] = field(default_factory=list)
    bank_lines: list[BankLine] = field(default_factory=list)
    links: list[TruthLink] = field(default_factory=list)
    exceptions: list[TruthException] = field(default_factory=list)
    settlements: dict[str, Settlement] = field(default_factory=dict)
    _touched: set[str] = field(default_factory=set)
    _seq: dict[str, int] = field(default_factory=dict)

    # -- id minting ---------------------------------------------------------
    def nid(self, prefix: str, width: int = 6) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}_{self._seq[prefix]:0{width}d}"

    # -- indexes ------------------------------------------------------------
    def order(self, order_id: str) -> Order | None:
        return self._order_ix.get(order_id)

    def entry(self, entry_id: str) -> PGEntry | None:
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    def bank_line(self, line_id: str) -> BankLine | None:
        return next((b for b in self.bank_lines if b.line_id == line_id), None)

    def settlement_row(self, settlement_id: str) -> PGEntry | None:
        """The gateway's own aggregate row for a settlement."""
        return next((e for e in self.entries
                     if e.entry_type is EntryType.SETTLEMENT
                     and e.settlement_id == settlement_id), None)

    def entries_of(self, settlement_id: str) -> list[PGEntry]:
        return [e for e in self.entries
                if e.settlement_id == settlement_id
                and e.entry_type is not EntryType.SETTLEMENT]

    @property
    def _order_ix(self) -> dict[str, Order]:
        return {o.order_id: o for o in self.orders}

    # -- injector bookkeeping ----------------------------------------------
    def claim(self, subject_id: str) -> bool:
        """Reserve a record for one injector, so two injectors never corrupt
        the same subject and produce an uninterpretable ground truth."""
        if subject_id in self._touched:
            return False
        self._touched.add(subject_id)
        return True

    def is_claimed(self, subject_id: str) -> bool:
        return subject_id in self._touched

    def flag(self, subject_type: str, subject_id: str, code: ExceptionCode,
             notes: str = "") -> None:
        self.exceptions.append(TruthException(
            subject_type=subject_type,
            subject_id=subject_id,
            code=code,
            resolvability=EXCEPTION_META[code].resolvability,
            notes=notes,
        ))

    def drop_links(self, **match) -> None:
        self.links = [
            l for l in self.links
            if not all(getattr(l, k) == v for k, v in match.items())
        ]


def business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _pick_amount(rng: random.Random) -> Paise:
    lo, hi, _ = rng.choices(AMOUNT_BANDS, weights=[b[2] for b in AMOUNT_BANDS])[0]
    return rupees(rng.randint(lo, hi))


def _stamp(rng: random.Random, d: date) -> datetime:
    return datetime.combine(d, time(rng.randint(6, 23), rng.randint(0, 59),
                                    rng.randint(0, 59)))


def _utr(rng: random.Random, d: date, seq: int) -> str:
    """16-char UTR in the shape banks actually print."""
    return f"{BANK_CODE}N{d.strftime('%y%m%d')}{seq:05d}"


# ---------------------------------------------------------------------------
# stage 1: the clean world
# ---------------------------------------------------------------------------

def _make_orders(w: World) -> None:
    s = w.spec
    for _ in range(s.n_orders):
        day = config.WORLD_START + timedelta(days=w.rng.randrange(s.n_days))
        failed = w.rng.random() < s.order_fail_rate
        w.orders.append(Order(
            order_id=w.nid("ORD"),
            customer_id=f"CUST_{w.rng.randrange(1, max(2, s.n_orders // 3)):05d}",
            amount=_pick_amount(w.rng),
            status=OrderStatus.FAILED if failed else OrderStatus.PAID,
            created_at=_stamp(w.rng, day),
        ))
    w.orders.sort(key=lambda o: o.created_at)


def _make_payments(w: World) -> None:
    methods = list(config.METHOD_WEIGHTS)
    weights = [config.METHOD_WEIGHTS[m] for m in methods]
    for o in w.orders:
        if o.status is OrderStatus.FAILED:
            # Normal noise: an ERP order with no gateway entry, and correctly
            # so. A matcher must not flag these as breaks.
            continue
        method = w.rng.choices(methods, weights=weights)[0]
        fee, tax, net = fees.payment_net(method, o.amount)
        w.entries.append(PGEntry(
            entry_id=w.nid("ENT"),
            entry_type=EntryType.PAYMENT,
            order_id=o.order_id,
            payment_id=w.nid("PAY"),
            method=method,
            gross=o.amount,
            fee=fee, tax=tax, net=net,
            created_at=o.created_at + timedelta(seconds=w.rng.randint(20, 900)),
        ))


def _make_refunds(w: World) -> None:
    payments = [e for e in w.entries if e.entry_type is EntryType.PAYMENT]
    n = round(w.spec.refund_rate * len(payments))
    for p in w.rng.sample(payments, min(n, len(payments))):
        # A partial refund is the common real case and it breaks any matcher
        # that assumes refund amount == order amount.
        full = w.rng.random() < 0.6
        amount = p.gross if full else rupees(
            round(p.gross * w.rng.uniform(0.2, 0.8) / 100, 2))
        f, t, net = fees.refund_net(p.method or "upi", amount)
        w.entries.append(PGEntry(
            entry_id=w.nid("ENT"),
            entry_type=EntryType.REFUND,
            order_id=p.order_id,
            payment_id=p.payment_id,
            method=p.method,
            gross=amount, fee=f, tax=t, net=net,
            created_at=p.created_at + timedelta(days=w.rng.randint(0, 12),
                                                seconds=w.rng.randint(0, 86399)),
            narration=f"Refund against {p.payment_id}",
        ))
        order = w.order(p.order_id or "")
        if order is not None and full:
            order.status = OrderStatus.REFUNDED


def _make_chargebacks(w: World) -> None:
    payments = [e for e in w.entries if e.entry_type is EntryType.PAYMENT]
    n = round(w.spec.chargeback_rate * len(payments))
    for p in w.rng.sample(payments, min(n, len(payments))):
        f, t, net = fees.chargeback_net(p.gross)
        w.entries.append(PGEntry(
            entry_id=w.nid("ENT"),
            entry_type=EntryType.CHARGEBACK,
            order_id=p.order_id,
            payment_id=p.payment_id,
            method=p.method,
            gross=p.gross, fee=f, tax=t, net=net,
            created_at=p.created_at + timedelta(days=w.rng.randint(15, 45)),
            narration=f"Chargeback raised on {p.payment_id}",
        ))


def _make_adjustments(w: World) -> None:
    """Benign, explained adjustments. The opaque ones are injected as E12."""
    for _ in range(max(2, w.spec.n_orders // 120)):
        day = config.WORLD_START + timedelta(days=w.rng.randrange(w.spec.n_days))
        amount = rupees(w.rng.randint(200, 9_000))
        credit = w.rng.random() < 0.5
        w.entries.append(PGEntry(
            entry_id=w.nid("ENT"),
            entry_type=EntryType.ADJUSTMENT,
            gross=amount,
            net=amount if credit else -amount,
            created_at=_stamp(w.rng, day),
            narration="Goodwill credit for support ticket"
                      if credit else "Recovery of excess payout",
        ))


SETTLEABLE = (EntryType.PAYMENT, EntryType.REFUND,
              EntryType.CHARGEBACK, EntryType.ADJUSTMENT)


def _settle(w: World) -> None:
    """Batch entries into T+2 business-day settlements and pay them out.

    Two real behaviours are modelled here rather than idealised away:

    * If a cycle's refunds and chargebacks exceed its inflows the payout would
      be negative. Gateways do not claw money out of the merchant's bank in
      that case -- the deficit carries into the next cycle. So a settlement
      batch is not always one day's traffic, and any matcher that assumes
      "settlement == one T+2 bucket" will be wrong on those days.
    * Entries whose settlement date falls past the statement horizon are left
      unsettled -- a real, non-erroneous state that the E06 injector labels.
    """
    horizon = config.WORLD_START + timedelta(days=w.spec.n_days)
    buckets: dict[date, list[PGEntry]] = {}
    for e in w.entries:
        if e.entry_type not in SETTLEABLE:
            continue
        sd = business_day(e.created_at.date()
                          + timedelta(days=config.SETTLEMENT_CYCLE_DAYS))
        if sd > horizon:
            continue
        buckets.setdefault(sd, []).append(e)

    pending_release: dict[date, Paise] = {}
    pending: list[PGEntry] = []      # deficit carried from earlier cycles
    seq = 0
    for sd in sorted(buckets):
        pending.extend(buckets[sd])

        # A reserve held RESERVE_RELEASE_DAYS ago is released into this payout.
        due = business_day(sd - timedelta(days=config.RESERVE_RELEASE_DAYS))
        release = pending_release.pop(due, 0)
        if release:
            rel = PGEntry(
                entry_id=w.nid("ENT"), entry_type=EntryType.RESERVE_RELEASE,
                net=release, created_at=_stamp(w.rng, sd),
                narration="Rolling reserve release",
            )
            w.entries.append(rel)
            pending.append(rel)

        members = list(pending)
        gross_total = sum(e.gross for e in members
                          if e.entry_type is EntryType.PAYMENT)
        net_sum = sum(e.net for e in members)
        tds = fees.tds_on(gross_total)
        held = fees.reserve_on(max(0, net_sum - tds))
        payout = net_sum - tds - held
        if payout <= 0:
            continue                 # nothing leaves the bank; roll forward

        pending.clear()
        seq += 1
        sid = w.nid("STL", 4)
        for e in members:
            e.settlement_id = sid
            e.settled_at = sd
        if tds:
            w.entries.append(PGEntry(
                entry_id=w.nid("ENT"), entry_type=EntryType.TDS, net=-tds,
                created_at=_stamp(w.rng, sd), settlement_id=sid, settled_at=sd,
                narration="TDS u/s 194-O on settlement gross"))
        if held:
            w.entries.append(PGEntry(
                entry_id=w.nid("ENT"), entry_type=EntryType.RESERVE_HOLD,
                net=-held, created_at=_stamp(w.rng, sd), settlement_id=sid,
                settled_at=sd, narration="Rolling reserve hold"))
            pending_release[sd] = held

        st = Settlement(
            settlement_id=sid, value_date=sd, utr=_utr(w.rng, sd, seq),
            member_entry_ids=[e.entry_id for e in members],
            gross_total=gross_total, tds=tds, reserve_held=held, payout=payout,
        )
        w.settlements[sid] = st
        w.entries.append(PGEntry(
            entry_id=w.nid("ENT"), entry_type=EntryType.SETTLEMENT, net=-payout,
            created_at=_stamp(w.rng, sd), settlement_id=sid, settled_at=sd,
            narration=f"Settlement payout UTR {st.utr}"))


def _make_bank_lines(w: World) -> None:
    for st in sorted(w.settlements.values(),
                     key=lambda s: (s.value_date, s.settlement_id)):
        line = BankLine(
            line_id=w.nid("LINE"),
            value_date=st.value_date,
            narration=(f"NEFT CR-{BANK_CODE}0000123-RAZORPAY SOFTWARE PVT LTD"
                       f"-{st.utr}-SETTLEMENT"),
            utr=st.utr,
            credit=st.payout,
        )
        st.line_id = line.line_id
        w.bank_lines.append(line)

    # Non-gateway noise the reconciler must ignore without flagging: rent,
    # salaries, bank charges. Distractors, not exceptions.
    for _ in range(max(3, w.spec.n_orders // 60)):
        d = business_day(config.WORLD_START
                         + timedelta(days=w.rng.randrange(w.spec.n_days)))
        label, amt = w.rng.choice([
            ("VENDOR PAYOUT-AWS INDIA", w.rng.randint(20_000, 180_000)),
            ("SALARY DISBURSEMENT BATCH", w.rng.randint(200_000, 900_000)),
            ("BANK CHARGES-NEFT", w.rng.randint(20, 300)),
            ("GST PAYMENT CHALLAN", w.rng.randint(10_000, 90_000)),
        ])
        w.bank_lines.append(BankLine(
            line_id=w.nid("LINE"), value_date=d,
            narration=f"DR-{label}", debit=rupees(amt)))


def _verify_identity(w: World) -> None:
    """The project's foundation: on a clean world, every settlement ties."""
    for st in w.settlements.values():
        members = w.entries_of(st.settlement_id)
        net = sum(e.net for e in members)
        line = w.bank_line(st.line_id or "")
        assert line is not None, f"{st.settlement_id} has no bank line"
        assert net == st.payout, (
            f"{st.settlement_id}: member nets {net} != payout {st.payout}")
        assert line.credit == st.payout, (
            f"{st.settlement_id}: bank credit {line.credit} != payout {st.payout}")


def _build_links(w: World) -> None:
    """Normalised ground truth: three edge types, long format."""
    for e in w.entries:
        if e.order_id:
            w.links.append(TruthLink(link_type="order_entry",
                                     order_id=e.order_id, entry_id=e.entry_id))
        if e.settlement_id and e.entry_type is not EntryType.SETTLEMENT:
            w.links.append(TruthLink(link_type="entry_settlement",
                                     entry_id=e.entry_id,
                                     settlement_id=e.settlement_id))
    for st in w.settlements.values():
        w.links.append(TruthLink(link_type="settlement_bank",
                                 settlement_id=st.settlement_id,
                                 line_id=st.line_id))


def _verify_signs(w: World) -> None:
    """Post-injection sanity: money still points the right way.

    A credit column cannot hold a negative number and a gateway cannot pay out
    a negative amount. Both were possible while injectors could bump a batch
    below zero, and the failure was invisible on large profiles.
    """
    for b in w.bank_lines:
        assert b.credit >= 0, f"{b.line_id} has a negative credit: {b.credit}"
        assert b.debit >= 0, f"{b.line_id} has a negative debit: {b.debit}"
    for st in w.settlements.values():
        assert st.payout > 0, f"{st.settlement_id} pays out {st.payout}"


def _recompute_balances(w: World) -> None:
    """Run after injection, so injected lines carry a coherent balance column."""
    w.bank_lines.sort(key=lambda b: (b.value_date, b.line_id))
    bal = OPENING_BALANCE
    for b in w.bank_lines:
        bal += b.credit - b.debit
        b.balance = bal


def build(spec: ScenarioSpec) -> World:
    from .injectors import inject_all

    w = World(spec=spec, rng=random.Random(spec.seed))
    _make_orders(w)
    _make_payments(w)
    _make_refunds(w)
    _make_chargebacks(w)
    _make_adjustments(w)
    _settle(w)
    _make_bank_lines(w)
    _verify_identity(w)      # clean world proven before anything is broken
    _build_links(w)
    inject_all(w)
    _verify_signs(w)         # injection must not invert any sign
    _recompute_balances(w)
    w.entries.sort(key=lambda e: (e.created_at, e.entry_id))
    return w
