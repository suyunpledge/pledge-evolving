from decimal import Decimal

from pricing import line_total


def build_report(orders):
    totals = {}
    for order in orders:
        status = order.get("status")
        if status not in ("paid", "refunded"):
            # cancelled (and any non-billable status) is skipped
            continue
        currency = order["currency"]
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        running = totals.get(currency, Decimal("0.00"))
        totals[currency] = running + amount if status == "paid" else running - amount
    return {c: f"{totals[c]:.2f}" for c in sorted(totals)}
