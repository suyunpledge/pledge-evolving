from decimal import Decimal

from pricing import line_total


def build_report(orders):
    totals = {}
    for order in orders:
        status = order.get("status")
        if status == "cancelled":
            continue
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        currency = order["currency"]
        running = totals.get(currency, Decimal("0.00"))
        if status == "refunded":
            running -= amount
        else:
            running += amount
        totals[currency] = running
    return {currency: format(totals[currency], ".2f") for currency in sorted(totals)}
