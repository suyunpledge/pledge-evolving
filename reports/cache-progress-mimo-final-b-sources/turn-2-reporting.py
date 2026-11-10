from decimal import Decimal

from pricing import line_total


def build_report(orders, only_currency=None):
    totals = {}
    for order in orders:
        status = order.get("status")
        if status not in ("paid", "refunded"):
            # cancelled (and any non-billable status) is skipped
            continue
        currency = order["currency"]
        if only_currency is not None and currency != only_currency:
            # filter to the exact requested currency only
            continue
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        running = totals.get(currency, Decimal("0.00"))
        totals[currency] = running + amount if status == "paid" else running - amount
    return {c: f"{totals[c]:.2f}" for c in sorted(totals)}
