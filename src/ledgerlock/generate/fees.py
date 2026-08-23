"""The fee engine. The pipeline recomputes fees with these same functions, so
any drift between a recomputed fee and the gateway's stated fee is either a
rounding artefact or a genuine discrepancy -- never a modelling difference.
"""

from __future__ import annotations

from .. import config
from ..domain.money import Paise, apply_rate


def mdr_fee(method: str, gross: Paise) -> Paise:
    return apply_rate(gross, config.MDR_RATES[method])


def gst_on_fee(fee: Paise) -> Paise:
    return apply_rate(fee, config.GST_RATE)


def tds_on(gross: Paise) -> Paise:
    return apply_rate(gross, config.TDS_RATE)


def reserve_on(net: Paise) -> Paise:
    return apply_rate(net, config.ROLLING_RESERVE_PCT)


def payment_net(method: str, gross: Paise) -> tuple[Paise, Paise, Paise]:
    """-> (fee, tax, net) for a captured payment."""
    fee = mdr_fee(method, gross)
    tax = gst_on_fee(fee)
    return fee, tax, gross - fee - tax


def refund_net(method: str, refund_amount: Paise) -> tuple[Paise, Paise, Paise]:
    """-> (fee, tax, net) for a refund.

    With REFUND_RETURNS_MDR false the merchant loses the full gross and never
    gets the original MDR back, so the net hit is bigger than the sale value.
    """
    if config.REFUND_RETURNS_MDR:
        fee = -mdr_fee(method, refund_amount)
        tax = -gst_on_fee(-fee)
        return fee, tax, -(refund_amount + fee + tax)
    return 0, 0, -refund_amount


def chargeback_net(amount: Paise) -> tuple[Paise, Paise, Paise]:
    fee = config.CHARGEBACK_FEE_PAISE
    tax = gst_on_fee(fee)
    return fee, tax, -(amount + fee + tax)
