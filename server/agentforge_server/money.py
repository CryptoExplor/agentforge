"""Bounded, exact mock-money arithmetic, independent of the ambient context.

Storage remains decimal strings (80 characters), not floats or SQL casts. The
supported operands span at most 80 integer/fractional positions each; 200 digits
cover their exact sums. Unrepresentable results fail closed, never round.
"""
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, Rounded, localcontext
from typing import Any

ZERO = Decimal("0")
MAX_MONEY_CHARS = 80


def _decimal(value: Any) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ValueError("money must be an exact decimal string, not a float or boolean")
    if not isinstance(value, Decimal):
        value = str(value)
        if len(value) > MAX_MONEY_CHARS + 1:
            raise ValueError("money is too long")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid decimal value") from exc
    if not number.is_finite():
        raise ValueError("amount must be finite")
    if (len(number.as_tuple().digits) > 200 or
            abs(number.as_tuple().exponent) > 80 or abs(number.adjusted()) > 80):
        raise ValueError("money precision is out of bounds")
    if len(format(number.copy_abs(), "f")) > MAX_MONEY_CHARS:
        raise ValueError("money exceeds storage precision")
    return number


def dec(value: Any) -> Decimal:
    number = _decimal(value)
    if number < ZERO:
        raise ValueError("amount must be finite and non-negative")
    return number


def money_string(value: Decimal) -> str:
    number = _decimal(value)
    rendered = format(number, "f")
    if len(rendered) > MAX_MONEY_CHARS:
        raise ValueError("money exceeds storage precision")
    return rendered


def money_sum(*values: Decimal) -> Decimal:
    operands = [_decimal(value) for value in values]
    # Do not inherit a caller's precision, rounding mode or disabled traps.
    context = Context(prec=200, traps=[InvalidOperation, Inexact, Rounded, Overflow])
    with localcontext(context):
        result = sum(operands, ZERO)
    money_string(result)  # Enforce the same storage bound on SQLite and PostgreSQL.
    return result


def fee_at_bps(amount: Decimal, bps: int) -> Decimal:
    """Exact basis-point share of ``amount`` (never rounds, fails closed).

    Division by 10000 (2^4 * 5^4) always terminates in decimal, so with ample
    precision the Inexact trap can only fire on a caller bug or an amount at
    the storage boundary — in which case settlement aborts instead of
    silently rounding marketplace fees.
    """
    number = _decimal(amount)
    if isinstance(bps, bool) or not isinstance(bps, int):
        raise ValueError("bps must be an integer")
    if not 0 <= bps <= 10000:
        raise ValueError("bps must be between 0 and 10000")
    context = Context(prec=200, traps=[InvalidOperation, Inexact, Rounded, Overflow])
    with localcontext(context):
        result = number * Decimal(bps) / Decimal(10000)
    money_string(result)  # Enforce the same storage bound on SQLite and PostgreSQL.
    return result
