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
