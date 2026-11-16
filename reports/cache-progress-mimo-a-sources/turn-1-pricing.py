def line_total(unit_price, quantity, discount="0"):
    return round(float(unit_price) * quantity * (1 - float(discount) / 100), 2)
