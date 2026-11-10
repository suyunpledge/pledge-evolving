from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_QUANTUM = Decimal("0.01")


def _to_decimal(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{name} must be finite")
        return Decimal(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be a valid number") from exc
        if not parsed.is_finite():
            raise ValueError(f"{name} must be finite")
        return parsed
    raise ValueError(f"{name} must be a number")


def line_total(unit_price, quantity, discount="0"):
    price = _to_decimal(unit_price, "unit_price")
    rate = _to_decimal(discount, "discount")

    if not price.is_finite() or price < 0:
        raise ValueError("unit_price must be finite and non-negative")
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError("discount must be between 0 and 100")

    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")

    total = price * quantity * (Decimal(1) - rate / Decimal(100))
    return total.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
