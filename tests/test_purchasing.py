"""Procure-to-pay, and the three-way match that guards it."""

import pytest

from src.domain.errors import ConflictError, MatchError, ValidationError


async def _fresh_order(erp, quantity=10, price=1000):
    order = await erp.purchasing.create_order(
        "Northwind Components",
        "MAIN",
        [{"item": "SKU-100", "quantity": quantity, "unit_price_minor": price}],
        order_date="2026-03-01",
    )
    await erp.purchasing.confirm_order(order["po_number"])
    return order


async def test_a_draft_order_cannot_be_received(erp):
    order = await erp.purchasing.create_order(
        "Northwind Components",
        "MAIN",
        [{"item": "SKU-100", "quantity": 5, "unit_price_minor": 1000}],
        order_date="2026-03-01",
    )
    with pytest.raises(ConflictError, match="confirm the order"):
        await erp.purchasing.receive(order["po_number"])


async def test_receipt_moves_stock_and_fills_grni(erp):
    order = await _fresh_order(erp, quantity=10, price=1000)
    grni_before = (await erp.accounts.balance("2150"))["balance_minor"]

    receipt = await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")
    grni_after = (await erp.accounts.balance("2150"))["balance_minor"]

    assert receipt["journal_entry"]["total_minor"] == 100_00
    assert grni_after - grni_before == 100_00, "received goods sit in GRNI until billed"
    assert (await erp.reports.trial_balance())["balanced"]


async def test_billing_clears_grni_into_payables(erp):
    order = await _fresh_order(erp, quantity=8, price=1250)
    await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")

    grni_before = (await erp.accounts.balance("2150"))["balance_minor"]
    ap_before = (await erp.accounts.balance("2000"))["balance_minor"]
    bill = await erp.purchasing.bill(order["po_number"], bill_date="2026-03-06")

    assert (await erp.accounts.balance("2150"))["balance_minor"] == grni_before - 100_00
    assert (await erp.accounts.balance("2000"))["balance_minor"] == ap_before + 100_00
    assert bill["matched"] is True


async def test_match_rejects_a_price_above_the_purchase_order(erp):
    order = await _fresh_order(erp, quantity=10, price=1000)
    await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")

    with pytest.raises(MatchError) as excinfo:
        await erp.purchasing.bill(
            order["po_number"],
            lines=[{"line_no": 1, "quantity": 10, "unit_price_minor": 1400}],
            bill_date="2026-03-06",
        )
    checks = {d["check"] for d in excinfo.value.discrepancies}
    assert "price" in checks


async def test_match_rejects_billing_more_than_was_received(erp):
    order = await _fresh_order(erp, quantity=10, price=1000)
    await erp.purchasing.receive(
        order["po_number"], lines=[{"line_no": 1, "quantity": 4}], receipt_date="2026-03-05"
    )

    with pytest.raises(MatchError) as excinfo:
        await erp.purchasing.bill(
            order["po_number"],
            lines=[{"line_no": 1, "quantity": 10, "unit_price_minor": 1000}],
            bill_date="2026-03-06",
        )
    checks = {d["check"] for d in excinfo.value.discrepancies}
    assert "quantity" in checks


async def test_a_failed_match_reports_every_discrepancy_at_once(erp):
    order = await _fresh_order(erp, quantity=10, price=1000)
    await erp.purchasing.receive(
        order["po_number"], lines=[{"line_no": 1, "quantity": 3}], receipt_date="2026-03-05"
    )
    result = await erp.purchasing.match_bill(
        order["po_number"],
        lines=[{"line_no": 1, "quantity": 9, "unit_price_minor": 1900}],
    )
    assert result["matched"] is False
    assert {d["check"] for d in result["discrepancies"]} == {"price", "quantity"}


async def test_a_failed_match_posts_nothing(erp):
    """The whole point of a Unit of Work: a rejected bill leaves no trace."""
    order = await _fresh_order(erp, quantity=10, price=1000)
    await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")
    before = await erp.purchasing.search_bills(limit=1)

    with pytest.raises(MatchError):
        await erp.purchasing.bill(
            order["po_number"],
            lines=[{"line_no": 1, "quantity": 10, "unit_price_minor": 9999}],
        )

    after = await erp.purchasing.search_bills(limit=1)
    assert after["total"] == before["total"]


async def test_receiving_more_than_ordered_is_refused(erp):
    order = await _fresh_order(erp, quantity=5, price=1000)
    with pytest.raises(ConflictError, match="left to receive"):
        await erp.purchasing.receive(order["po_number"], lines=[{"line_no": 1, "quantity": 9}])


async def test_paying_more_than_outstanding_is_refused(erp):
    order = await _fresh_order(erp, quantity=2, price=1000)
    await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")
    bill = await erp.purchasing.bill(order["po_number"], bill_date="2026-03-06")

    with pytest.raises(ConflictError, match="exceeds the outstanding"):
        await erp.purchasing.pay_bill(bill["bill_number"], amount_minor=50_00)


async def test_partial_payment_leaves_the_bill_open(erp):
    order = await _fresh_order(erp, quantity=10, price=1000)
    await erp.purchasing.receive(order["po_number"], receipt_date="2026-03-05")
    bill = await erp.purchasing.bill(order["po_number"], bill_date="2026-03-06")

    await erp.purchasing.pay_bill(bill["bill_number"], amount_minor=40_00)
    reloaded = await erp.purchasing.get_bill(bill["bill_number"])

    assert reloaded["status"] == "posted"
    assert reloaded["outstanding_minor"] == 60_00

    await erp.purchasing.pay_bill(bill["bill_number"])
    settled = await erp.purchasing.get_bill(bill["bill_number"])
    assert settled["status"] == "paid"
    assert settled["outstanding_minor"] == 0


async def test_a_customer_cannot_be_used_as_a_supplier(erp):
    with pytest.raises(ValidationError, match="not a supplier"):
        await erp.purchasing.create_order(
            "Rivet & Co",
            "MAIN",
            [{"item": "SKU-100", "quantity": 1, "unit_price_minor": 100}],
        )


async def test_a_misspelled_price_field_is_refused_rather_than_priced_at_zero(erp):
    with pytest.raises(ValidationError, match="unrecognised field"):
        await erp.purchasing.create_order(
            "Northwind Components",
            "MAIN",
            [{"item": "SKU-100", "quantity": 100, "unit_price": 450}],
            order_date="2026-03-01",
        )


async def test_an_order_line_must_carry_a_price(erp):
    with pytest.raises(ValidationError, match="unit_price_minor is required"):
        await erp.purchasing.create_order(
            "Northwind Components",
            "MAIN",
            [{"item": "SKU-100", "quantity": 100}],
            order_date="2026-03-01",
        )
