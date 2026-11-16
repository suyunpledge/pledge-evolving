from decimal import Decimal

from pricing import line_total

ZERO = Decimal("0")
CENTS = Decimal("0.01")


def build_report(orders):
    """Summarize orders into {currency: total-as-two-decimal-string}.

    Skips cancelled orders; paid orders add their rounded line total,
    refunded orders subtract theirs. Results are sorted by currency and
    input rows are never modified.
    """
    totals = {}
    for order in orders:
        status = order.get("status", "paid")
        if status == "cancelled" or status not in ("paid", "refunded"):
            continue
        amount = line_total(order["price"], order["quantity"], order.get("discount", "0"))
        if status == "refunded":
            amount = -amount
        currency = order["currency"]
        totals[currency] = totals.get(currency, ZERO) + amount
    return {
        currency: str(totals[currency].quantize(CENTS))
        for currency in sorted(totals)
    }
