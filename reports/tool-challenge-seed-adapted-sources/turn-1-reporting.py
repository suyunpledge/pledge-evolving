from decimal import Decimal

from pricing import line_total


def build_report(orders):
    totals = {}
    for order in orders:
        status = order.get("status", "paid")
        if status == "cancelled":
            continue

        line = line_total(
            order["price"],
            order["quantity"],
            order.get("discount", "0"),
        )
        if status == "refunded":
            line = -line

        currency = order["currency"]
        totals[currency] = totals.get(currency, Decimal("0")) + line

    return {
        currency: format(totals[currency], ".2f")
        for currency in sorted(totals)
    }
