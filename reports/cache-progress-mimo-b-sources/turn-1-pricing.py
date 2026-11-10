from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")


def _to_decimal(value, name):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"nonfinite {name}: {value!r}")
    return number


def line_total(unit_price, quantity, discount="0"):
    price = _to_decimal(unit_price, "unit_price")
    rate = _to_decimal(discount, "discount")
    if price < 0:
        raise ValueError(f"negative unit_price: {unit_price!r}")
    if not (Decimal("0") <= rate <= _HUNDRED):
        raise ValueError(f"discount outside 0..100: {discount!r}")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError(f"quantity must be a positive integer: {quantity!r}")
    if quantity <= 0:
        raise ValueError(f"quantity must be a positive integer: {quantity!r}")
    total = price * quantity * (_HUNDRED - rate) / _HUNDRED
    return total.quantize(_CENT, rounding=ROUND_HALF_UP)
