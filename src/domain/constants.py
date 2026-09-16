"""Pure domain constants — no I/O, no dependencies."""

# Each account type's normal (positive) side. Adding a type is data, not a branch.
NORMAL_SIDE: dict[str, str] = {
    "asset": "debit",
    "expense": "debit",
    "liability": "credit",
    "equity": "credit",
    "income": "credit",
}

ACCOUNT_TYPES: frozenset[str] = frozenset(NORMAL_SIDE)
DIRECTIONS: frozenset[str] = frozenset({"debit", "credit"})

PARTNER_KINDS: frozenset[str] = frozenset({"customer", "supplier", "both"})
ITEM_KINDS: frozenset[str] = frozenset({"stockable", "service"})

# Roles let posting rules name accounts by meaning rather than by code, so a
# deployment can renumber its chart of accounts without touching any service.
ACCOUNT_ROLES: frozenset[str] = frozenset(
    {"bank", "ar", "ap", "inventory", "grni", "cogs", "revenue", "expense", "equity"}
)

# Which partner kinds may act in each role.
SUPPLIER_KINDS: frozenset[str] = frozenset({"supplier", "both"})
CUSTOMER_KINDS: frozenset[str] = frozenset({"customer", "both"})
