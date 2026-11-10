from decimal import Decimal

from pricing import line_total


def build_report(orders, only_currency=None):
    totals = {}
    for order in orders:
        status = order.get("status", "paid")
        if status == "cancelled":
            continue
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        currency = order["currency"]
        if only_currency is not None and currency != only_currency:
            continue
        current = totals.get(currency, Decimal("0.00"))
        if status == "refunded":
            current -= amount
        else:
            current += amount
        totals[currency] = current
    return {currency: str(totals[currency].quantize(Decimal("0.01"))) for currency in sorted(totals)}
