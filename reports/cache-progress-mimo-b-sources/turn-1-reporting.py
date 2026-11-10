from decimal import Decimal, ROUND_HALF_UP

from pricing import line_total

_CENT = Decimal("0.01")


def build_report(orders):
    totals = {}
    for order in orders:
        status = order.get("status", "paid")
        if status == "cancelled":
            continue
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        if status == "refunded":
            amount = -amount
        currency = order["currency"]
        totals[currency] = totals.get(currency, Decimal("0")) + amount
    return {
        currency: str(totals[currency].quantize(_CENT, rounding=ROUND_HALF_UP))
        for currency in sorted(totals)
    }
