from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_CENT = Decimal("0.01")


def _to_decimal(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not bool")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"invalid {name}: {value!r}")
    if not d.is_finite():
        raise ValueError(f"nonfinite {name}: {value!r}")
    return d


def line_total(unit_price, quantity, discount="0"):
    price = _to_decimal(unit_price, "unit_price")
    disc = _to_decimal(discount, "discount")

    if price < 0:
        raise ValueError(f"negative unit_price: {unit_price!r}")
    if disc < 0 or disc > 100:
        raise ValueError(f"discount outside 0..100: {discount!r}")

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError(f"invalid quantity: {quantity!r}")

    total = price * quantity * (Decimal(1) - disc / Decimal(100))
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)
