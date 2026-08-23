"""Record schemas for the three sources and the two ground-truth files.

Sign convention for PGEntry.net is "effect on the merchant's gateway balance":
money arriving is positive, money leaving (refunds, chargebacks, tax withheld,
reserve held, and the settlement payout itself) is negative.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .taxonomy import ExceptionCode, Resolvability


class EntryType(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    CHARGEBACK = "chargeback"
    ADJUSTMENT = "adjustment"
    TDS = "tds"
    RESERVE_HOLD = "rolling_reserve_hold"
    RESERVE_RELEASE = "rolling_reserve_release"
    SETTLEMENT = "settlement"


class OrderStatus(StrEnum):
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Order(_Rec):
    """The merchant's own record. Deliberately knows nothing about fees."""
    order_id: str
    customer_id: str
    amount: int                # paise, gross value of the sale
    status: OrderStatus
    created_at: datetime


class PGEntry(_Rec):
    """One line of the gateway ledger."""
    entry_id: str
    entry_type: EntryType
    order_id: str | None = None      # None for adjustments, tds, reserve, settlement
    payment_id: str | None = None
    method: str | None = None
    gross: int = 0
    fee: int = 0
    tax: int = 0                     # GST on the fee
    net: int = 0                     # signed effect on gateway balance
    settlement_id: str | None = None # None while unsettled
    created_at: datetime
    settled_at: date | None = None
    narration: str = ""


class BankLine(_Rec):
    """One line of the bank statement, as messy as a bank statement is.

    `utr` is what the bank's own parser managed to extract -- often nothing.
    The authoritative copy of the UTR is buried in `narration`.
    """
    line_id: str
    value_date: date
    narration: str
    utr: str = ""
    credit: int = 0
    debit: int = 0
    balance: int = 0


class TruthLink(_Rec):
    """One true edge in the reconciliation graph. Long format: a merged
    settlement is simply two rows sharing a line_id."""
    link_type: str
    order_id: str | None = None
    entry_id: str | None = None
    settlement_id: str | None = None
    line_id: str | None = None


class TruthException(_Rec):
    subject_type: str          # order | entry | bank_line | settlement
    subject_id: str
    code: ExceptionCode
    resolvability: Resolvability
    notes: str = ""
