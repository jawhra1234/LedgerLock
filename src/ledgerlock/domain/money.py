"""Money is an integer number of paise. There are no floats in this project.

A float would make the +/-Rs 0.01 drift exceptions accidental instead of
deliberate, which would quietly invalidate every accuracy number we publish.
Decimal appears only at the fee-calculation boundary, where a real rate has to
be applied to a real amount, and the result is rounded once, explicitly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

Paise = int


def rupees(amount: str | int | Decimal) -> Paise:
    """Rs 123.45 -> 12345 paise."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_rate(base: Paise, rate: Decimal) -> Paise:
    """Apply a rate to a paise amount, rounding half-up to the nearest paisa.

    Half-up (not banker's rounding) is what Indian payment processors use on
    fee lines, so this is the behaviour a matcher has to reproduce.
    """
    return int((Decimal(base) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(amount: Paise) -> str:
    """12345 -> 'Rs 123.45', with Indian digit grouping."""
    sign = "-" if amount < 0 else ""
    whole, frac = divmod(abs(amount), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{sign}Rs {s}.{frac:02d}"
