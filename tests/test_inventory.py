"""Stock quantity and stock value are one thing. These tests hold them together."""

import pytest

from src.domain.errors import ConflictError, ValidationError
from src.money import unit_cost


async def test_inventory_account_equals_stock_valuation(erp):
    """The single most important cross-check in the system: what the warehouse
    says it holds must equal what the balance sheet says it is worth."""
    valuation = await erp.inventory.valuation()
    account = await erp.accounts.balance("1300")
    assert valuation["total_value_minor"] == account["balance_minor"], (
        f"stock value {valuation['total_value']} != Inventory account {account['balance']}"
    )


async def test_average_cost_moves_with_each_receipt(erp):
    """Two receipts at different prices average out; the third figure is the
    weighted mean, not the latest price."""
    await erp.items.create("SKU-AVCO", "Average cost probe", "stockable", "unit", 0)
    await erp.inventory.adjust("SKU-AVCO", "MAIN", 10, reason="Opening", unit_cost_minor=1000)
    item = await erp.items.get("SKU-AVCO")
    assert item["unit_cost_minor"] == 1000

    await erp.inventory.adjust("SKU-AVCO", "MAIN", 10, reason="Second lot", unit_cost_minor=2000)
    item = await erp.items.get("SKU-AVCO")
    assert item["on_hand_milli"] == 20_000
    assert item["unit_cost_minor"] == 1500, "20 units at $10 and $20 average $15"


async def test_emptying_the_pool_leaves_no_residual_value(erp):
    """Repeated rounding must never strand a cent on a zero-quantity item."""
    await erp.items.create("SKU-ODD", "Awkward divisor", "stockable", "unit", 0)
    await erp.inventory.adjust("SKU-ODD", "MAIN", 3, reason="Opening", unit_cost_minor=1000)
    await erp.inventory.adjust("SKU-ODD", "MAIN", -1, reason="Issue one")
    await erp.inventory.adjust("SKU-ODD", "MAIN", -2, reason="Issue the rest")

    item = await erp.items.get("SKU-ODD")
    assert item["on_hand_milli"] == 0
    assert item["value_minor"] == 0, "an empty warehouse is worth nothing"


async def test_stock_cannot_go_negative(erp):
    await erp.items.create("SKU-SHORT", "Short stock", "stockable", "unit", 0)
    await erp.inventory.adjust("SKU-SHORT", "MAIN", 2, reason="Opening", unit_cost_minor=500)
    with pytest.raises(ConflictError, match="Insufficient stock"):
        await erp.inventory.adjust("SKU-SHORT", "MAIN", -5, reason="Too many")


async def test_services_are_never_stocked(erp):
    with pytest.raises(ValidationError, match="service"):
        await erp.inventory.adjust(
            "SVC-010", "MAIN", 5, reason="Cannot stock labour", unit_cost_minor=100
        )


async def test_adjustment_posts_its_value_to_the_ledger(erp):
    """Counting stock without booking the value change is how inventory and the
    balance sheet drift apart."""
    before = await erp.accounts.balance("1300")
    await erp.items.create("SKU-POST", "Posting probe", "stockable", "unit", 0)
    result = await erp.inventory.adjust(
        "SKU-POST", "MAIN", 4, reason="Found in the back", unit_cost_minor=2500
    )
    after = await erp.accounts.balance("1300")

    assert result["journal_entry"]["total_minor"] == 100_00
    assert after["balance_minor"] - before["balance_minor"] == 100_00
    assert (await erp.reports.trial_balance())["balanced"]


def test_unit_cost_rounds_half_up_without_floats():
    assert unit_cost(1000, 3000) == 333
    assert unit_cost(0, 0) == 0
    assert unit_cost(100, 3000) == 33
