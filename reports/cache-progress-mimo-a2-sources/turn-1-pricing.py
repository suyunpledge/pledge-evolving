from decimal import Decimal, ROUND_HALF_UP, InvalidOperation


CENT = Decimal("0.01")


def _to_decimal(value, name):
    """Convert via Decimal(str(value)); reject bools, malformed and nonfinite input."""
    if isinstance(value, bool):
        raise ValueError("invalid %s" % name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        raise ValueError("invalid %s" % name)
    if not result.is_finite():
        raise ValueError("nonfinite %s" % name)
    return result


def line_total(unit_price, quantity, discount="0"):
    price = _to_decimal(unit_price, "unit_price")
    if price < 0:
        raise ValueError("negative unit_price")

    disc = _to_decimal(discount, "discount")
    if disc < 0 or disc > 100:
        raise ValueError("discount out of range")

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("invalid quantity")

    total = price * quantity * (Decimal(1) - disc / Decimal(100))
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
