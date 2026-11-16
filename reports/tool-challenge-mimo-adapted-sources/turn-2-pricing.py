from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENTS = Decimal("0.01")
PERCENT = Decimal(100)


def _to_decimal(value, name):
    """Convert value via Decimal(str(value)); reject anything non-finite."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be finite: {value!r}")
    return number


def line_total(unit_price, quantity, discount="0"):
    """Price one line: unit_price * quantity, minus discount percent,
    rounded half-up to the nearest cent, as an exact Decimal."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError(f"quantity must be a positive integer: {quantity!r}")
    price = _to_decimal(unit_price, "unit_price")
    if price < 0:
        raise ValueError(f"unit_price must not be negative: {unit_price!r}")
    rate = _to_decimal(discount, "discount")
    if rate < 0 or rate > 100:
        raise ValueError(f"discount must be between 0 and 100: {discount!r}")
    total = price * quantity * (PERCENT - rate) / PERCENT
    return total.quantize(CENTS, rounding=ROUND_HALF_UP)
