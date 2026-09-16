"""Money and quantity formatting — the single source of truth for rendering.

Amounts are stored and computed as integer minor units (cents); quantities as
integer milli-units (1000 = 1.000 unit). Both format through integer arithmetic
only (divmod), so there is no float rounding anywhere in the project. Every
layer must format through these helpers so rounding is identical everywhere.
"""

MILLI = 1000


def format_minor(amount_minor: int) -> str:
    """Render integer minor units (cents) as currency, e.g. -12345 -> "-$123.45"."""
    sign = "-" if amount_minor < 0 else ""
    dollars, cents = divmod(abs(int(amount_minor)), 100)
    return f"{sign}${dollars:,}.{cents:02d}"


def format_qty(qty_milli: int) -> str:
    """Render integer milli-units as a quantity, e.g. 2500 -> "2.5", 3000 -> "3"."""
    sign = "-" if qty_milli < 0 else ""
    whole, fraction = divmod(abs(int(qty_milli)), MILLI)
    if fraction == 0:
        return f"{sign}{whole:,}"
    return f"{sign}{whole:,}.{fraction:03d}".rstrip("0")


def to_milli(quantity: float | int) -> int:
    """Convert a caller-supplied quantity to milli-units, rounding half-up.

    Callers at the transport boundary may pass 2.5; storage is always integer.
    """
    return int(round(float(quantity) * MILLI))


def extend(qty_milli: int, unit_minor: int) -> int:
    """Line total for a quantity at a unit price: exact, rounded half-up.

    Integer-only: (qty * price + 500) // 1000 rounds a half-cent up without
    ever touching a float.
    """
    return (qty_milli * unit_minor + MILLI // 2) // MILLI


def unit_cost(value_minor: int, qty_milli: int) -> int:
    """Average unit cost from a total value and quantity, rounded half-up."""
    if qty_milli <= 0:
        return 0
    return (value_minor * MILLI + qty_milli // 2) // qty_milli
