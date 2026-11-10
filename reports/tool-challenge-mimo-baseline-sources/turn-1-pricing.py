from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_QUANTUM = Decimal("0.01")


def _to_decimal(value, name):
    """Convert via Decimal(str(value)); reject bool/non-numeric/nonfinite input."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number")
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return number


def line_total(unit_price, quantity, discount="0"):
    price = _to_decimal(unit_price, "unit_price")
    rate = _to_decimal(discount, "discount")
    if price < 0:
        raise ValueError("unit_price must be non-negative")
    if not (Decimal("0") <= rate <= Decimal("100")):
        raise ValueError("discount must be between 0 and 100")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    total = price * quantity * (Decimal("1") - rate / Decimal("100"))
    return total.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
