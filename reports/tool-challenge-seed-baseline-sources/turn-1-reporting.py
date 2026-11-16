from pricing import line_total

def build_report(orders):
    totals = {}
    for order in orders:
        currency = order["currency"]
        totals[currency] = totals.get(currency, 0) + line_total(order["price"], order["quantity"], order.get("discount", "0"))
    return {k: str(v) for k, v in totals.items()}
