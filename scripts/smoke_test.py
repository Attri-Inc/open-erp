#!/usr/bin/env python3
"""End-to-end smoke test over the MCP tool surface.

The unit tests call services; this calls the tools exactly as an agent would,
so a transport-layer mistake (a bad envelope, a tool wired to the wrong service)
fails the build even when the services themselves are fine.

Runs a full procure-to-pay and order-to-cash cycle on the seeded database and
checks the books balance at the end.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MCP_TRANSPORT", "stdio")

from src import mcp_server as tools  # noqa: E402
from src.container import container  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        FAILURES.append(label)


def payload(raw: str) -> dict:
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        raise AssertionError(f"tool returned an error: {data['error']}")
    return data


async def main() -> int:
    print("OpenERP smoke test")

    print("\nMaster data")
    partners = payload(await tools.list_partners())
    check("list_partners returns the seeded book", len(partners) >= 4, f"{len(partners)} partners")
    items = payload(await tools.list_items())
    check("list_items returns products and services", len(items) >= 4, f"{len(items)} items")
    item = payload(await tools.get_item("SKU-100"))
    check("get_item reports on-hand per warehouse", "by_warehouse" in item)

    print("\nProcure-to-pay")
    order = payload(
        await tools.create_purchase_order(
            "Northwind Components",
            "MAIN",
            [{"item": "SKU-200", "quantity": 50, "unit_price_minor": 700}],
            order_date="2026-05-01",
        )
    )
    po = order["po_number"]
    check("create_purchase_order drafts an order", order["status"] == "draft", po)
    payload(await tools.confirm_purchase_order(po))
    receipt = payload(await tools.receive_goods(po, receipt_date="2026-05-04"))
    check(
        "receive_goods posts a journal entry",
        receipt["journal_entry"]["total_minor"] == 350_00,
        receipt["value"],
    )

    mismatch = payload(
        await tools.match_vendor_bill(
            po, lines=[{"line_no": 1, "quantity": 50, "unit_price_minor": 900}]
        )
    )
    check(
        "match_vendor_bill catches an overpriced invoice",
        mismatch["matched"] is False,
        f"{len(mismatch['discrepancies'])} discrepancy(ies)",
    )

    blocked = json.loads(
        await tools.post_vendor_bill(
            po, lines=[{"line_no": 1, "quantity": 50, "unit_price_minor": 900}]
        )
    )
    check("post_vendor_bill refuses an unmatched invoice", "error" in blocked)
    check("the refusal explains every discrepancy", bool(blocked.get("discrepancies")))

    bill = payload(await tools.post_vendor_bill(po, bill_date="2026-05-05"))
    check("post_vendor_bill posts a matched invoice", bill["matched"] is True, bill["bill_number"])
    paid = payload(await tools.pay_vendor_bill(bill["bill_number"], payment_date="2026-05-20"))
    check("pay_vendor_bill settles it", paid["status"] == "paid", paid["amount"])

    print("\nOrder-to-cash")
    sales_order = payload(
        await tools.create_sales_order(
            "Meridian Retail",
            "MAIN",
            [{"item": "SKU-200", "quantity": 20, "unit_price_minor": 1500}],
            order_date="2026-05-06",
        )
    )
    so = sales_order["so_number"]
    payload(await tools.confirm_sales_order(so))
    delivery = payload(await tools.deliver_goods(so, delivery_date="2026-05-08"))
    check(
        "deliver_goods books cost of goods sold",
        delivery["journal_entry"]["total_minor"] > 0,
        delivery["cost_of_goods_sold"],
    )
    invoice = payload(await tools.post_customer_invoice(so, invoice_date="2026-05-09"))
    check(
        "post_customer_invoice recognises revenue",
        invoice["journal_entry"]["total_minor"] == 300_00,
        invoice["invoice_number"],
    )
    receipted = payload(
        await tools.receive_customer_payment(
            invoice["invoice_number"], amount_minor=100_00, payment_date="2026-05-25"
        )
    )
    check(
        "receive_customer_payment applies a part payment",
        receipted["status"] == "posted",
        f"outstanding {receipted['outstanding_after']}",
    )

    print("\nInventory")
    valuation = payload(await tools.get_stock_valuation())
    on_hand = payload(await tools.get_stock_on_hand())
    check(
        "get_stock_valuation totals the pool",
        valuation["total_value_minor"] > 0,
        valuation["total_value"],
    )
    check(
        "get_stock_on_hand reports by warehouse", on_hand["count"] > 0, f"{on_hand['count']} lines"
    )

    inventory_account = payload(await tools.get_balance("1300"))
    check(
        "stock value equals the Inventory account",
        valuation["total_value_minor"] == inventory_account["balance_minor"],
        f"{valuation['total_value']} vs {inventory_account['balance']}",
    )

    print("\nReports")
    trial = payload(await tools.get_trial_balance())
    check(
        "trial balance balances",
        trial["balanced"] is True,
        f"{trial['total_debit']} / {trial['total_credit']}",
    )
    sheet = payload(await tools.get_balance_sheet())
    check("balance sheet balances", sheet["balanced"] is True, sheet["total_assets"])
    pnl = payload(await tools.get_profit_and_loss())
    check("profit & loss computes", "net_profit" in pnl, pnl["net_profit"])
    ar = payload(await tools.get_ar_aging())
    ap = payload(await tools.get_ap_aging())
    check("AR aging buckets open invoices", ar["total_minor"] >= 0, ar["total"])
    check("AP aging buckets open bills", ap["total_minor"] >= 0, ap["total"])
    grni = payload(await tools.get_unbilled_receipts())
    check("GRNI reports the match backlog", "grni_balance" in grni, grni["grni_balance"])

    print("\nLedger integrity")
    entry = payload(
        await tools.post_journal_entry(
            "2026-05-30",
            "Smoke-test accrual",
            [
                {"account": "6000", "direction": "debit", "amount_minor": 2500},
                {"account": "2000", "direction": "credit", "amount_minor": 2500},
            ],
        )
    )
    reversed_entry = payload(await tools.reverse_entry(entry["entry_number"]))
    check(
        "reverse_entry writes a contra entry", reversed_entry["contra_entry"]["total_minor"] == 2500
    )
    after = payload(await tools.get_trial_balance())
    check("books still balance after a reversal", after["balanced"] is True)

    unbalanced = json.loads(
        await tools.post_journal_entry(
            "2026-05-30",
            "Deliberately lopsided",
            [
                {"account": "1010", "direction": "debit", "amount_minor": 100},
                {"account": "3000", "direction": "credit", "amount_minor": 90},
            ],
        )
    )
    check("an unbalanced entry is refused", "error" in unbalanced)

    print("\nAudit & query")
    audit = payload(await tools.get_audit_log(limit=5))
    check("audit log records every mutation", audit["total"] > 0, f"{audit['total']} rows")
    query = payload(await tools.run_query("SELECT COUNT(*) AS n FROM journal_entries"))
    check("run_query answers a read", query["rows"][0]["n"] > 0)
    blocked_write = json.loads(await tools.run_query("DELETE FROM journal_lines"))
    check("run_query refuses a write", "error" in blocked_write)

    await container.close()

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
