"""Order-to-cash: shipping at cost, invoicing what shipped, collecting cash."""

import pytest

from src.domain.errors import ConflictError, ValidationError


async def _stocked_order(erp, quantity=5, price=3000):
    order = await erp.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-100", "quantity": quantity, "unit_price_minor": price}],
        order_date="2026-04-01",
    )
    await erp.sales.confirm_order(order["so_number"])
    return order


async def test_a_draft_order_cannot_ship(erp):
    order = await erp.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-100", "quantity": 1, "unit_price_minor": 3000}],
        order_date="2026-04-01",
    )
    with pytest.raises(ConflictError, match="confirm the order"):
        await erp.sales.deliver(order["so_number"])


async def test_delivery_books_cost_at_average_not_at_price(erp):
    """COGS is what the goods cost us, never what we charged for them."""
    order = await _stocked_order(erp, quantity=4, price=9999)
    item = await erp.items.get("SKU-100")
    expected_cost = item["unit_cost_minor"] * 4 // 1

    cogs_before = (await erp.accounts.balance("5000"))["balance_minor"]
    delivery = await erp.sales.deliver(order["so_number"], delivery_date="2026-04-02")
    cogs_after = (await erp.accounts.balance("5000"))["balance_minor"]

    assert cogs_after - cogs_before == delivery["journal_entry"]["total_minor"]
    assert abs(delivery["journal_entry"]["total_minor"] - expected_cost) <= 4
    assert (await erp.reports.trial_balance())["balanced"]


async def test_invoicing_recognises_revenue_and_the_receivable(erp):
    order = await _stocked_order(erp, quantity=3, price=5000)
    await erp.sales.deliver(order["so_number"], delivery_date="2026-04-02")

    ar_before = (await erp.accounts.balance("1100"))["balance_minor"]
    revenue_before = (await erp.accounts.balance("4000"))["balance_minor"]
    invoice = await erp.sales.invoice(order["so_number"], invoice_date="2026-04-03")

    assert invoice["journal_entry"]["total_minor"] == 150_00
    assert (await erp.accounts.balance("1100"))["balance_minor"] == ar_before + 150_00
    assert (await erp.accounts.balance("4000"))["balance_minor"] == revenue_before + 150_00


async def test_nothing_can_be_invoiced_before_it_ships(erp):
    order = await _stocked_order(erp, quantity=2)
    with pytest.raises(ConflictError, match="Nothing left to invoice"):
        await erp.sales.invoice(order["so_number"])


async def test_shipping_more_than_is_on_hand_is_refused(erp):
    await erp.items.create("SKU-SCARCE", "Scarce widget", "stockable", "unit", 5000)
    await erp.inventory.adjust("SKU-SCARCE", "MAIN", 3, reason="Opening", unit_cost_minor=1000)
    order = await erp.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-SCARCE", "quantity": 10, "unit_price_minor": 5000}],
        order_date="2026-04-01",
    )
    await erp.sales.confirm_order(order["so_number"])

    with pytest.raises(ConflictError, match="Insufficient stock"):
        await erp.sales.deliver(order["so_number"])


async def test_a_refused_delivery_moves_no_stock(erp):
    await erp.items.create("SKU-ROLLBACK", "Rollback probe", "stockable", "unit", 5000)
    await erp.inventory.adjust("SKU-ROLLBACK", "MAIN", 2, reason="Opening", unit_cost_minor=1000)
    order = await erp.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-ROLLBACK", "quantity": 5, "unit_price_minor": 5000}],
        order_date="2026-04-01",
    )
    await erp.sales.confirm_order(order["so_number"])

    with pytest.raises(ConflictError):
        await erp.sales.deliver(order["so_number"])

    item = await erp.items.get("SKU-ROLLBACK")
    assert item["on_hand_milli"] == 2000, "the failed shipment must leave stock untouched"


async def test_partial_shipment_then_partial_invoice(erp):
    order = await _stocked_order(erp, quantity=6, price=2000)
    await erp.sales.deliver(
        order["so_number"], lines=[{"line_no": 1, "quantity": 2}], delivery_date="2026-04-02"
    )
    invoice = await erp.sales.invoice(order["so_number"], invoice_date="2026-04-03")

    assert invoice["journal_entry"]["total_minor"] == 40_00, "invoice only what shipped"
    reloaded = await erp.sales.get_order(order["so_number"])
    assert reloaded["status"] == "confirmed", "still open — four units to go"


async def test_a_supplier_cannot_be_used_as_a_customer(erp):
    with pytest.raises(ValidationError, match="not a customer"):
        await erp.sales.create_order(
            "Northwind Components",
            "MAIN",
            [{"item": "SKU-100", "quantity": 1, "unit_price_minor": 100}],
        )


async def test_receipt_clears_the_receivable(erp):
    order = await _stocked_order(erp, quantity=2, price=4000)
    await erp.sales.deliver(order["so_number"], delivery_date="2026-04-02")
    invoice = await erp.sales.invoice(order["so_number"], invoice_date="2026-04-03")

    ar_before = (await erp.accounts.balance("1100"))["balance_minor"]
    bank_before = (await erp.accounts.balance("1010"))["balance_minor"]
    await erp.sales.receive_payment(invoice["invoice_number"], payment_date="2026-04-20")

    assert (await erp.accounts.balance("1100"))["balance_minor"] == ar_before - 80_00
    assert (await erp.accounts.balance("1010"))["balance_minor"] == bank_before + 80_00
    assert (await erp.sales.get_invoice(invoice["invoice_number"]))["status"] == "paid"


async def test_aging_buckets_a_long_overdue_invoice(erp):
    aging = await erp.reports.ar_aging()
    assert aging["total_minor"] > 0
    assert any(line["bucket"] != "current" for line in aging["lines"]), (
        "the seeded 70-day-old invoice must land in an overdue bucket"
    )


async def test_a_misspelled_price_field_is_refused_on_a_sales_order(erp):
    with pytest.raises(ValidationError, match="unrecognised field"):
        await erp.sales.create_order(
            "Rivet & Co",
            "MAIN",
            [{"item": "SKU-100", "quantity": 3, "unit_price": 2999}],
            order_date="2026-03-01",
        )


async def test_an_omitted_sales_price_still_falls_back_to_the_list_price(erp):
    order = await erp.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-100", "quantity": 3}],
        order_date="2026-03-01",
    )
    assert order["total_minor"] > 0
