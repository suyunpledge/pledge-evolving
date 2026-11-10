from decimal import Decimal

from pricing import line_total


def build_report(orders):
    totals = {}
    for order in orders:
        if order.get("status") == "cancelled":
            continue
        currency = order["currency"]
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        if order.get("status") == "refunded":
            amount = -amount
        totals[currency] = totals.get(currency, Decimal("0")) + amount

    return {
        currency: str(totals[currency].quantize(Decimal("0.01")))
        for currency in sorted(totals)
    }
