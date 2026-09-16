"""ID generation and the system clock — infrastructure concerns kept out of the
pure domain so services can stay deterministic-friendly."""

import uuid
from datetime import UTC, date, datetime, timedelta


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def parse_date(value: str, field: str = "date") -> date:
    """Parse an ISO date, raising the domain's ValidationError on bad input."""
    from src.domain.errors import ValidationError

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError(f"Invalid {field} '{value}': expected YYYY-MM-DD") from exc


def add_days(value: str, days: int) -> str:
    """ISO date plus N days — used for payment terms."""
    return (parse_date(value) + timedelta(days=days)).isoformat()
