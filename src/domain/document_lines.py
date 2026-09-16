"""Validation for the line dicts that arrive on order and bill tools.

An agent composing a line is guessing at key names, and a guess that lands on an
unrecognised key must not pass silently: an ignored ``unit_price`` would book a
priced order at zero. Every caller therefore declares the keys its line accepts,
and anything outside that set is rejected by name.
"""

from src.domain.errors import ValidationError


def validate_line_keys(line: dict, index: int, allowed: frozenset[str]) -> None:
    unknown = sorted(set(line) - allowed)
    if unknown:
        raise ValidationError(
            f"Line {index}: unrecognised field(s) {', '.join(unknown)} — "
            f"expected any of {', '.join(sorted(allowed))}"
        )


def require_line_field(line: dict, index: int, field: str) -> None:
    if field not in line:
        raise ValidationError(f"Line {index}: {field} is required")
