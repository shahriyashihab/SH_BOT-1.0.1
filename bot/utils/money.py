"""Money helpers - consistent rounding at every balance write boundary.

The bot stores balances as Python ``float`` (SQLite REAL columns throughout).
Binary floats cannot represent most decimal money values exactly, so repeated
arithmetic accumulates a tiny drift (e.g. 0.1 + 0.2 -> 0.30000000000000004).
Storing unrounded floats let that drift compound over tens of thousands of
credits/deductions/refunds until a balance no longer matches its ledger.

``round_money`` clamps every value to 2 decimal places (cents) at the point it
is written to storage, which bounds the error to < 0.005 for every operation
and keeps balances equal to their exact cent value. It is the single choke
point every money write should pass through.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENTS = Decimal("0.01")


def round_money(value, default: float = 0.0) -> float:
    """Return ``value`` rounded to 2 decimals (cents) as a float.

    Non-numeric / NaN / infinite input returns ``default``. Uses Decimal
    ROUND_HALF_UP so 2.675 -> 2.68 (not banker's rounding).
    """
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if not d.is_finite():
        return default
    return float(d.quantize(_CENTS, rounding=ROUND_HALF_UP))
