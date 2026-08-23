"""Read-only projections over the raw sources.

The gateway does not hand over a settlement object -- it hands over a ledger
with a `settlement` row per batch, and the UTR buried in that row's narration.
SettlementView is the reconciler rebuilding that batch from the ledger alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..domain.models import BankLine, EntryType, Order, PGEntry
from ..domain.money import Paise
from ..io.loaders import Sources

# "Settlement payout UTR HDFCN26060400002"
_UTR_IN_ROW = re.compile(r"UTR\s+([A-Z0-9]+)", re.I)


@dataclass
class SettlementView:
    settlement_id: str
    payout: Paise                  # what the gateway says it paid out
    utr: str | None                # parsed from the settlement row's narration
    value_date: date | None
    row: PGEntry                   # the aggregate row itself
    members: list[PGEntry] = field(default_factory=list)

    @property
    def member_net(self) -> Paise:
        """What the members actually add up to. Should equal `payout`."""
        return sum(e.net for e in self.members)

    @property
    def ties_internally(self) -> bool:
        return self.member_net == self.payout


def build_settlements(entries: list[PGEntry]) -> dict[str, SettlementView]:
    rows = {e.settlement_id: e for e in entries
            if e.entry_type is EntryType.SETTLEMENT and e.settlement_id}
    views: dict[str, SettlementView] = {}
    for sid, row in rows.items():
        m = _UTR_IN_ROW.search(row.narration or "")
        views[sid] = SettlementView(
            settlement_id=sid,
            payout=-row.net,           # the row is the mirror of the payout
            utr=m.group(1) if m else None,
            value_date=row.settled_at,
            row=row,
        )
    for e in entries:
        if e.entry_type is EntryType.SETTLEMENT:
            continue
        if e.settlement_id and e.settlement_id in views:
            views[e.settlement_id].members.append(e)
    return views


@dataclass
class Index:
    """Every lookup the tiers need, built once."""
    sources: Sources
    orders: dict[str, Order] = field(default_factory=dict)
    entries: dict[str, PGEntry] = field(default_factory=dict)
    bank: dict[str, BankLine] = field(default_factory=dict)
    entries_by_order: dict[str, list[PGEntry]] = field(default_factory=dict)
    settlements: dict[str, SettlementView] = field(default_factory=dict)

    @classmethod
    def build(cls, src: Sources) -> Index:
        ix = cls(sources=src)
        ix.orders = {o.order_id: o for o in src.orders}
        ix.entries = {e.entry_id: e for e in src.entries}
        ix.bank = {b.line_id: b for b in src.bank_lines}
        for e in src.entries:
            if e.order_id:
                ix.entries_by_order.setdefault(e.order_id, []).append(e)
        ix.settlements = build_settlements(src.entries)
        return ix

    @property
    def credit_lines(self) -> list[BankLine]:
        return [b for b in self.sources.bank_lines if b.credit > 0]
