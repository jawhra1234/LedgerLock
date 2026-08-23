from decimal import Decimal

import pytest

from ledgerlock.domain.money import apply_rate, fmt, rupees
from ledgerlock.domain import fees


def test_rupees_to_paise():
    assert rupees("123.45") == 12345
    assert rupees(0) == 0
    assert rupees("0.01") == 1


def test_apply_rate_is_half_up_not_bankers():
    # 0.5 paise must go up. Banker's rounding would give 2 here and put every
    # fee line half a paisa adrift from what the gateway actually charged.
    assert apply_rate(50, Decimal("0.05")) == 3        # 2.5 -> 3
    assert apply_rate(150, Decimal("0.05")) == 8       # 7.5 -> 8


def test_fmt_uses_indian_grouping():
    assert fmt(48_721_350) == "Rs 4,87,213.50"
    assert fmt(-100) == "-Rs 1.00"
    assert fmt(0) == "Rs 0.00"


@pytest.mark.parametrize("gross", [99, 100_00, 1_23_456, 250_000_00])
def test_payment_net_never_loses_a_paisa(gross):
    fee, tax, net = fees.payment_net("card", gross)
    assert fee + tax + net == gross


def test_gst_is_charged_on_the_fee_not_the_gross():
    fee, tax, _ = fees.payment_net("card", 100_000)
    assert fee == 2_000                      # 2% MDR
    assert tax == 360                        # 18% of the fee, not of the gross


def test_refund_does_not_return_the_mdr():
    """The merchant loses the full sale value and never gets the fee back."""
    _, _, pay_net = fees.payment_net("card", 100_000)
    _, _, refund_net = fees.refund_net("card", 100_000)
    assert pay_net + refund_net < 0          # net loss even on a full reversal
    assert refund_net == -100_000


def test_upi_is_zero_mdr():
    fee, tax, net = fees.payment_net("upi", 500_00)
    assert (fee, tax) == (0, 0)
    assert net == 500_00
