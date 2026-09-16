#!/usr/bin/env python3
"""Bootstrap a fresh OpenERP database with a small, complete business.

The seed runs the real services, not raw INSERTs, so every seeded document
passes the same validation, posts the same journal entries and writes the same
audit rows as a live one. If the seed can produce it, a user can too.

It leaves the books mid-cycle on purpose: some bills unpaid, some goods
received but not billed, one order shipped but not invoiced — so the aging and
GRNI reports have something to show.
"""

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH  # noqa: E402
from src.container import Container  # noqa: E402

SCHEMA = ROOT / "scripts" / "schema.sql"

# Roles are what posting rules bind to; codes are cosmetic and a deployment
# may renumber them freely.
ACCOUNTS = [
    ("1010", "Bank", "asset", "bank"),
    ("1100", "Accounts receivable", "asset", "ar"),
    ("1300", "Inventory", "asset", "inventory"),
    ("2000", "Accounts payable", "liability", "ap"),
    ("2150", "Goods received not invoiced", "liability", "grni"),
    ("3000", "Owner's equity", "equity", "equity"),
    ("4000", "Sales revenue", "income", "revenue"),
    ("5000", "Cost of goods sold", "expense", "cogs"),
    ("6000", "Operating expenses", "expense", "expense"),
]

SEQUENCES = [
    ("purchase_order", "PO-", 4),
    ("goods_receipt", "GRN-", 4),
    ("vendor_bill", "BILL-", 4),
    ("sales_order", "SO-", 4),
    ("delivery", "DN-", 4),
    ("customer_invoice", "INV-", 4),
    ("payment", "PAY-", 4),
    ("journal_entry", "JE-", 4),
]

PARTNERS = [
    ("SUP-001", "Northwind Components", "supplier", "orders@northwind.example", 30),
    ("SUP-002", "Harbour Packaging", "supplier", "ap@harbourpack.example", 14),
    ("CUS-001", "Rivet & Co", "customer", "purchasing@rivet.example", 30),
    ("CUS-002", "Meridian Retail", "customer", "accounts@meridian.example", 45),
]

ITEMS = [
    ("SKU-100", "Aluminium bracket", "stockable", "unit", 3400),
    ("SKU-200", "Steel hinge, 60mm", "stockable", "unit", 1250),
    ("SKU-300", "Shipping carton, large", "stockable", "unit", 220),
    ("SVC-010", "Assembly labour", "service", "hour", 8500),
]


def _bootstrap(path: str) -> None:
    """Create the schema and the rows services assume already exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if Path(path).exists():
        Path(path).unlink()
    for suffix in ("-wal", "-shm"):
        stale = Path(path + suffix)
        if stale.exists():
            stale.unlink()

    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.executemany(
        "INSERT INTO sequences (name, prefix, padding, next_value) VALUES (?,?,?,1)", SEQUENCES
    )
    conn.execute(
        "INSERT INTO org_settings (id, business_name, base_currency, price_tolerance_minor, "
        "created_at, updated_at) VALUES (1, ?, 'USD', 0, ?, ?)",
        ("Northgate Works", now, now),
    )
    conn.commit()
    conn.close()


def _day(offset: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=offset)).isoformat()


async def _seed(container: Container) -> None:
    for code, name, account_type, role in ACCOUNTS:
        await container.accounts.create(code, name, account_type, role, actor="seed")

    for code, name, kind, email, terms in PARTNERS:
        await container.partners.create(code, name, kind, email, None, terms, actor="seed")

    for sku, name, kind, uom, price in ITEMS:
        await container.items.create(sku, name, kind, uom, price, actor="seed")

    await container.warehouses.create("MAIN", "Main warehouse", actor="seed")
    await container.warehouses.create("OVERFLOW", "Overflow store", actor="seed")

    # Opening capital, so the bank has money to spend before anything is sold.
    await container.posting.post_adjustment(
        _day(-60),
        "Opening capital",
        [
            {"account": "1010", "direction": "debit", "amount_minor": 5_000_00},
            {"account": "3000", "direction": "credit", "amount_minor": 5_000_00},
        ],
        reference="OPENING",
        actor="seed",
    )

    # --- Procure-to-pay: received, billed and paid in full --------------------
    po1 = await container.purchasing.create_order(
        "Northwind Components",
        "MAIN",
        [
            {"item": "SKU-100", "quantity": 120, "unit_price_minor": 1800},
            {"item": "SKU-200", "quantity": 400, "unit_price_minor": 640},
        ],
        order_date=_day(-45),
        actor="seed",
    )
    await container.purchasing.confirm_order(po1["po_number"], actor="seed")
    await container.purchasing.receive(po1["po_number"], receipt_date=_day(-41), actor="seed")
    bill1 = await container.purchasing.bill(
        po1["po_number"], bill_date=_day(-40), supplier_ref="NW-88213", actor="seed"
    )
    await container.purchasing.pay_bill(
        bill1["bill_number"], payment_date=_day(-12), reference="ACH 4471", actor="seed"
    )

    # --- Procure-to-pay: billed but unpaid, so AP aging has a row ------------
    po2 = await container.purchasing.create_order(
        "Harbour Packaging",
        "MAIN",
        [{"item": "SKU-300", "quantity": 1000, "unit_price_minor": 95}],
        order_date=_day(-30),
        actor="seed",
    )
    await container.purchasing.confirm_order(po2["po_number"], actor="seed")
    await container.purchasing.receive(po2["po_number"], receipt_date=_day(-28), actor="seed")
    await container.purchasing.bill(
        po2["po_number"], bill_date=_day(-28), supplier_ref="HP-2291", actor="seed"
    )

    # --- Procure-to-pay: received but NOT billed, so GRNI holds a balance ----
    po3 = await container.purchasing.create_order(
        "Northwind Components",
        "MAIN",
        [{"item": "SKU-200", "quantity": 250, "unit_price_minor": 680}],
        order_date=_day(-9),
        actor="seed",
    )
    await container.purchasing.confirm_order(po3["po_number"], actor="seed")
    await container.purchasing.receive(po3["po_number"], receipt_date=_day(-6), actor="seed")

    # --- Order-to-cash: delivered, invoiced and paid -------------------------
    so1 = await container.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [
            {"item": "SKU-100", "quantity": 40},
            {"item": "SKU-200", "quantity": 90},
        ],
        order_date=_day(-25),
        actor="seed",
    )
    await container.sales.confirm_order(so1["so_number"], actor="seed")
    await container.sales.deliver(so1["so_number"], delivery_date=_day(-23), actor="seed")
    inv1 = await container.sales.invoice(so1["so_number"], invoice_date=_day(-23), actor="seed")
    await container.sales.receive_payment(
        inv1["invoice_number"], payment_date=_day(-4), reference="Wire 90112", actor="seed"
    )

    # --- Order-to-cash: invoiced, overdue, unpaid — AR aging has a row -------
    so2 = await container.sales.create_order(
        "Meridian Retail",
        "MAIN",
        [
            {"item": "SKU-300", "quantity": 300, "unit_price_minor": 240},
            {"item": "SVC-010", "quantity": 6},
        ],
        order_date=_day(-70),
        actor="seed",
    )
    await container.sales.confirm_order(so2["so_number"], actor="seed")
    await container.sales.deliver(so2["so_number"], delivery_date=_day(-68), actor="seed")
    await container.sales.invoice(so2["so_number"], invoice_date=_day(-68), actor="seed")

    # --- Order-to-cash: shipped, not yet invoiced ----------------------------
    so3 = await container.sales.create_order(
        "Rivet & Co",
        "MAIN",
        [{"item": "SKU-100", "quantity": 25}],
        order_date=_day(-5),
        actor="seed",
    )
    await container.sales.confirm_order(so3["so_number"], actor="seed")
    await container.sales.deliver(so3["so_number"], delivery_date=_day(-3), actor="seed")

    # A count found two damaged brackets. Written off, value and all.
    await container.inventory.adjust(
        "SKU-100",
        "MAIN",
        -2,
        reason="Damaged in handling",
        adjusted_at=_day(-2),
        actor="seed",
    )


async def _report(container: Container) -> None:
    trial = await container.reports.trial_balance()
    sheet = await container.reports.balance_sheet()
    valuation = await container.inventory.valuation()
    grni = await container.reports.unbilled_receipts()
    ar = await container.reports.ar_aging()
    ap = await container.reports.ap_aging()

    print(
        f"  Trial balance   {trial['total_debit']} Dr / {trial['total_credit']} Cr "
        f"· balanced={trial['balanced']}"
    )
    print(f"  Balance sheet   assets {sheet['total_assets']} · balanced={sheet['balanced']}")
    print(f"  Stock value     {valuation['total_value']} across {len(valuation['lines'])} items")
    print(f"  GRNI (unbilled) {grni['grni_balance']}")
    print(f"  Receivables     {ar['total']} · Payables {ap['total']}")


async def _run() -> None:
    _bootstrap(DB_PATH)
    container = Container(DB_PATH)
    try:
        await _seed(container)
        print(f"Seeded {DB_PATH}")
        await _report(container)
    finally:
        await container.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
