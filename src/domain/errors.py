"""Typed domain errors.

Services raise these; the transport boundary (MCP tools) maps them to a stable
error envelope. A single hierarchy means every layer speaks the same language.
"""


class DomainError(Exception):
    """Base class for all expected, client-facing domain failures."""


class NotFoundError(DomainError):
    """A referenced entity does not exist (partner, item, order, ...)."""


class ValidationError(DomainError):
    """Input violates a domain rule (unbalanced entry, bad date, bad quantity)."""


class ConflictError(DomainError):
    """The operation conflicts with current state (duplicate code, wrong status)."""


class MatchError(DomainError):
    """A vendor bill failed three-way matching against its PO and receipts.

    Carries the individual failures so the caller sees every discrepancy at
    once rather than fixing them one round-trip at a time.
    """

    def __init__(self, message: str, discrepancies: list[dict] | None = None):
        super().__init__(message)
        self.discrepancies = discrepancies or []
