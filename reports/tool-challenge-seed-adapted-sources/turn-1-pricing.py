from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


def line_total(unit_price, quantity, discount="0"):
    # bool is a subclass of int, so it must be rejected explicitly.
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")

    try:
        price = Decimal(str(unit_price))
    except (InvalidOperation, ValueError):
        raise ValueError("unit_price must be a valid number")

    try:
        rate = Decimal(str(discount))
    except (InvalidOperation, ValueError):
        raise ValueError("discount must be a valid number")

    if not price.is_finite() or price < 0:
        raise ValueError("unit_price must be finite and non-negative")
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError("discount must be between 0 and 100")

    total = price * quantity * (1 - rate / Decimal("100"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
