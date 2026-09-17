"""OpenERP MCP Server — 47 tools for running a small business end to end.

This module is a thin transport adapter: each tool holds no business logic — it
calls a service (resolved from the composition root) and maps the result (or a
typed DomainError) to a JSON envelope via the serialization helpers.
"""

import functools
import os

from mcp.server.mcpserver import MCPServer

from src.config import MCP_PORT
from src.container import container
from src.domain.errors import DomainError, MatchError
from src.serialization import error_response, tool_response

mcp = MCPServer(
    "openerp",
    instructions="""
OpenERP is a local-first ERP: purchasing, inventory and sales, each posting to
a double-entry general ledger.

Two document chains carry everything:
  procure-to-pay  create_purchase_order -> confirm_purchase_order -> receive_goods
                  -> post_vendor_bill -> pay_vendor_bill
  order-to-cash   create_sales_order -> confirm_sales_order -> deliver_goods
                  -> post_customer_invoice -> receive_customer_payment

Money is integer minor units (cents): 100 = $1.00. Quantities are decimals
(2.5 is fine). Partners, items, warehouses and accounts resolve by id, code/sku
or name, so you can pass "Acme" or "SKU-001" directly.

A vendor bill only posts if it passes three-way matching against its purchase
order (price) and goods receipts (quantity) — use match_vendor_bill to check
before posting. Journal entries are immutable; correct them with reverse_entry.
""",
)


def _guard(func):
    """Map typed domain errors to the standard envelope so every tool reports
    failure the same way. MatchError additionally returns its discrepancies."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except MatchError as exc:
            import json

            return json.dumps({"error": str(exc), "discrepancies": exc.discrepancies})
        except DomainError as exc:
            return error_response(str(exc))

    return wrapper


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def list_partners(
    kind: str | None = None, q: str | None = None, include_archived: bool = False
) -> str:
    """List customers and suppliers. Filter by kind (customer, supplier, both)
    or search by name/code. Use for "who do we buy from?", "list our customers"."""
    return tool_response(await container.partners.list_partners(kind, include_archived, q))


@mcp.tool()
@_guard
async def get_partner(partner: str) -> str:
    """Get one partner by id, code, or name. Use for "tell me about Acme"."""
    return tool_response(await container.partners.get(partner))


@mcp.tool()
@_guard
async def create_partner(
    code: str,
    name: str,
    kind: str,
    email: str | None = None,
    phone: str | None = None,
    payment_terms_days: int = 30,
) -> str:
    """Add a customer or supplier. kind is customer, supplier, or both.
    payment_terms_days drives invoice and bill due dates."""
    return tool_response(
        await container.partners.create(code, name, kind, email, phone, payment_terms_days)
    )


@mcp.tool()
@_guard
async def list_items(
    kind: str | None = None, q: str | None = None, include_archived: bool = False
) -> str:
    """List products and services with on-hand quantity, average cost and stock value.
    Use for "what do we sell?", "show stockable items"."""
    return tool_response(await container.items.list_items(kind, include_archived, q))


@mcp.tool()
@_guard
async def get_item(item: str) -> str:
    """Get one item by id, sku, or name, with its on-hand quantity per warehouse.
    Use for "how many SKU-001 do we have and where?"."""
    return tool_response(await container.items.get(item))


@mcp.tool()
@_guard
async def create_item(
    sku: str, name: str, kind: str = "stockable", uom: str = "unit", sale_price_minor: int = 0
) -> str:
    """Add a product (kind=stockable) or a service (kind=service).
    Services are never stocked and post straight to expense when received."""
    return tool_response(await container.items.create(sku, name, kind, uom, sale_price_minor))


@mcp.tool()
@_guard
async def list_warehouses(include_archived: bool = False) -> str:
    """List stock locations. Use for "where do we hold inventory?"."""
    return tool_response(await container.warehouses.list_warehouses(include_archived))


@mcp.tool()
@_guard
async def create_warehouse(code: str, name: str) -> str:
    """Add a stock location."""
    return tool_response(await container.warehouses.create(code, name))


# ---------------------------------------------------------------------------
# Chart of accounts & ledger
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def list_accounts(account_type: str | None = None, include_archived: bool = False) -> str:
    """List the chart of accounts with balances. Filter by type
    (asset, liability, equity, income, expense)."""
    return tool_response(await container.accounts.list_accounts(account_type, include_archived))


@mcp.tool()
@_guard
async def get_balance(account: str, as_of: str | None = None) -> str:
    """Get an account balance by id, code (e.g. "1010") or name, optionally as of
    a date (YYYY-MM-DD). Positive on the account's normal side.
    Use for "how much cash do we have?"."""
    return tool_response(await container.accounts.balance(account, as_of))


@mcp.tool()
@_guard
async def get_account_ledger(
    account: str, date_from: str | None = None, date_to: str | None = None, limit: int = 100
) -> str:
    """Every posting to one account with a running balance, each row naming the
    document that caused it. Use for "show all Inventory activity"."""
    return tool_response(await container.accounts.ledger(account, date_from, date_to, limit))


@mcp.tool()
@_guard
async def create_account(
    code: str,
    name: str,
    account_type: str,
    role: str | None = None,
    description: str | None = None,
) -> str:
    """Add an account. `role` maps it to a posting rule — one of bank, ar, ap,
    inventory, grni, cogs, revenue, expense, equity — so documents can post
    without hard-coded account codes."""
    return tool_response(
        await container.accounts.create(code, name, account_type, role, description)
    )


@mcp.tool()
@_guard
async def get_journal_entry(entry: str) -> str:
    """Get one journal entry with all its lines, by id or entry number (e.g. "JE-0007")."""
    result = await container.posting.get_entry(entry)
    if not result:
        return error_response(f"Journal entry not found: {entry}")
    return tool_response(result)


@mcp.tool()
@_guard
async def search_journal(
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source_type: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Search the journal. Filter by text, date range, or source_type
    (goods_receipt, vendor_bill, supplier_payment, delivery, customer_invoice,
    customer_payment, adjustment, reversal)."""
    return tool_response(
        await container.posting.search(q, date_from, date_to, source_type, limit, offset)
    )


@mcp.tool()
@_guard
async def post_journal_entry(
    entry_date: str, description: str, lines: list[dict], reference: str | None = None
) -> str:
    """Post a manual journal entry — the escape hatch for accruals, opening
    balances and corrections. Lines are
    [{"account": "1010", "direction": "debit", "amount_minor": 5000, "memo": "..."}].
    Debits must equal credits."""
    return tool_response(
        await container.posting.post_adjustment(entry_date, description, lines, reference)
    )


@mcp.tool()
@_guard
async def reverse_entry(entry: str, entry_date: str | None = None) -> str:
    """Reverse a posted journal entry with a contra entry. The original is kept —
    this is how corrections happen, because postings are never edited."""
    return tool_response(await container.posting.reverse(entry, entry_date))


# ---------------------------------------------------------------------------
# Procure-to-pay
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def create_purchase_order(
    supplier: str,
    warehouse: str,
    lines: list[dict],
    order_date: str | None = None,
    note: str | None = None,
) -> str:
    """Raise a purchase order. Lines are
    [{"item": "SKU-001", "quantity": 10, "unit_price_minor": 1250}].
    Created as a draft — confirm it before receiving."""
    return tool_response(
        await container.purchasing.create_order(supplier, warehouse, lines, order_date, note)
    )


@mcp.tool()
@_guard
async def confirm_purchase_order(order: str) -> str:
    """Commit a draft purchase order to the supplier."""
    return tool_response(await container.purchasing.confirm_order(order))


@mcp.tool()
@_guard
async def get_purchase_order(order: str) -> str:
    """Get a purchase order by id or number (e.g. "PO-0003") with its lines,
    received/billed quantities, and receipts."""
    return tool_response(await container.purchasing.get_order(order))


@mcp.tool()
@_guard
async def search_purchase_orders(
    supplier: str | None = None, status: str | None = None, limit: int = 25, offset: int = 0
) -> str:
    """Search purchase orders by supplier or status
    (draft, confirmed, received, billed, cancelled)."""
    return tool_response(await container.purchasing.search_orders(supplier, status, limit, offset))


@mcp.tool()
@_guard
async def receive_goods(
    order: str, lines: list[dict] | None = None, receipt_date: str | None = None
) -> str:
    """Book a supplier delivery: stock in at PO cost, value into GRNI.
    Omit `lines` to receive everything outstanding, or pass
    [{"line_no": 1, "quantity": 4}] for a partial receipt."""
    return tool_response(await container.purchasing.receive(order, lines, receipt_date))


@mcp.tool()
@_guard
async def match_vendor_bill(order: str, lines: list[dict] | None = None) -> str:
    """Dry-run the three-way match (PO price vs bill price, received vs billed
    quantity) without posting. Use before post_vendor_bill to see every
    discrepancy at once."""
    return tool_response(await container.purchasing.match_bill(order, lines))


@mcp.tool()
@_guard
async def post_vendor_bill(
    order: str,
    lines: list[dict] | None = None,
    bill_date: str | None = None,
    supplier_ref: str | None = None,
) -> str:
    """Post a supplier invoice against a purchase order. Runs the three-way match
    first and refuses to post if it fails. Omit `lines` to bill everything
    received and not yet billed."""
    return tool_response(await container.purchasing.bill(order, lines, bill_date, supplier_ref))


@mcp.tool()
@_guard
async def get_vendor_bill(bill: str) -> str:
    """Get a vendor bill by id or number (e.g. "BILL-0002") with lines and
    outstanding amount."""
    return tool_response(await container.purchasing.get_bill(bill))


@mcp.tool()
@_guard
async def search_vendor_bills(
    supplier: str | None = None, status: str | None = None, limit: int = 25, offset: int = 0
) -> str:
    """Search vendor bills by supplier or status (posted, paid, cancelled)."""
    return tool_response(await container.purchasing.search_bills(supplier, status, limit, offset))


@mcp.tool()
@_guard
async def pay_vendor_bill(
    bill: str,
    amount_minor: int | None = None,
    payment_date: str | None = None,
    reference: str | None = None,
) -> str:
    """Pay a supplier from the bank. Omit amount_minor to settle the full
    outstanding balance."""
    return tool_response(
        await container.purchasing.pay_bill(bill, amount_minor, payment_date, reference)
    )


# ---------------------------------------------------------------------------
# Order-to-cash
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def create_sales_order(
    customer: str,
    warehouse: str,
    lines: list[dict],
    order_date: str | None = None,
    note: str | None = None,
) -> str:
    """Take a customer order. Lines are
    [{"item": "SKU-001", "quantity": 3, "unit_price_minor": 2999}];
    omit unit_price_minor to use the item's list price."""
    return tool_response(
        await container.sales.create_order(customer, warehouse, lines, order_date, note)
    )


@mcp.tool()
@_guard
async def confirm_sales_order(order: str) -> str:
    """Confirm a draft sales order so it can be delivered."""
    return tool_response(await container.sales.confirm_order(order))


@mcp.tool()
@_guard
async def get_sales_order(order: str) -> str:
    """Get a sales order by id or number (e.g. "SO-0005") with delivered and
    invoiced quantities per line."""
    return tool_response(await container.sales.get_order(order))


@mcp.tool()
@_guard
async def search_sales_orders(
    customer: str | None = None, status: str | None = None, limit: int = 25, offset: int = 0
) -> str:
    """Search sales orders by customer or status
    (draft, confirmed, delivered, invoiced, cancelled)."""
    return tool_response(await container.sales.search_orders(customer, status, limit, offset))


@mcp.tool()
@_guard
async def deliver_goods(
    order: str, lines: list[dict] | None = None, delivery_date: str | None = None
) -> str:
    """Ship a sales order: stock out at average cost, cost booked to COGS.
    Refuses to ship more than is on hand. Omit `lines` to ship everything
    outstanding."""
    return tool_response(await container.sales.deliver(order, lines, delivery_date))


@mcp.tool()
@_guard
async def post_customer_invoice(
    order: str, lines: list[dict] | None = None, invoice_date: str | None = None
) -> str:
    """Invoice a customer for what has been delivered: revenue and receivable.
    Omit `lines` to invoice everything delivered and not yet invoiced."""
    return tool_response(await container.sales.invoice(order, lines, invoice_date))


@mcp.tool()
@_guard
async def get_customer_invoice(invoice: str) -> str:
    """Get a customer invoice by id or number (e.g. "INV-0004") with lines and
    outstanding amount."""
    return tool_response(await container.sales.get_invoice(invoice))


@mcp.tool()
@_guard
async def search_customer_invoices(
    customer: str | None = None, status: str | None = None, limit: int = 25, offset: int = 0
) -> str:
    """Search customer invoices by customer or status (posted, paid, cancelled)."""
    return tool_response(await container.sales.search_invoices(customer, status, limit, offset))


@mcp.tool()
@_guard
async def receive_customer_payment(
    invoice: str,
    amount_minor: int | None = None,
    payment_date: str | None = None,
    reference: str | None = None,
) -> str:
    """Apply a customer payment against an invoice. Omit amount_minor to settle
    the full outstanding balance."""
    return tool_response(
        await container.sales.receive_payment(invoice, amount_minor, payment_date, reference)
    )


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def get_stock_on_hand(item: str | None = None) -> str:
    """On-hand quantity by item and warehouse. Use for "what's in the warehouse?"."""
    return tool_response(await container.inventory.stock_on_hand(item))


@mcp.tool()
@_guard
async def get_stock_valuation() -> str:
    """Inventory value by item at average cost, and the total that should agree
    with the Inventory account on the balance sheet."""
    return tool_response(await container.inventory.valuation())


@mcp.tool()
@_guard
async def list_stock_moves(
    item: str | None = None,
    warehouse: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Every stock movement with its cost and the document behind it.
    Use for "why did SKU-001 stock change last week?"."""
    return tool_response(
        await container.inventory.list_moves(item, warehouse, date_from, date_to, limit, offset)
    )


@mcp.tool()
@_guard
async def adjust_stock(
    item: str,
    warehouse: str,
    quantity: float,
    reason: str = "Stock adjustment",
    adjusted_at: str | None = None,
    unit_cost_minor: int | None = None,
) -> str:
    """Correct on-hand stock after a count, posting the value change to the GL.
    A positive quantity adds stock, negative writes it off. Pass unit_cost_minor
    when adding stock for an item that has no cost yet."""
    return tool_response(
        await container.inventory.adjust(
            item, warehouse, quantity, reason, adjusted_at, unit_cost_minor
        )
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def get_trial_balance(as_of: str | None = None) -> str:
    """Trial balance: every account's debit/credit balance, with a `balanced`
    flag. Use for "are the books balanced?"."""
    return tool_response(await container.reports.trial_balance(as_of))


@mcp.tool()
@_guard
async def get_profit_and_loss(date_from: str | None = None, date_to: str | None = None) -> str:
    """Profit & loss for a period: income, expenses, net profit."""
    return tool_response(await container.reports.profit_and_loss(date_from, date_to))


@mcp.tool()
@_guard
async def get_balance_sheet(as_of: str | None = None) -> str:
    """Balance sheet: assets, liabilities and equity including current retained
    earnings, with a `balanced` flag."""
    return tool_response(await container.reports.balance_sheet(as_of))


@mcp.tool()
@_guard
async def get_ar_aging(as_of: str | None = None) -> str:
    """Receivables aging: who owes us, how much, and how overdue
    (current, 1-30, 31-60, 61-90, 90+)."""
    return tool_response(await container.reports.ar_aging(as_of))


@mcp.tool()
@_guard
async def get_ap_aging(as_of: str | None = None) -> str:
    """Payables aging: who we owe, how much, and how overdue."""
    return tool_response(await container.reports.ap_aging(as_of))


@mcp.tool()
@_guard
async def get_unbilled_receipts() -> str:
    """The GRNI balance — goods received that no supplier has billed for yet.
    This is the three-way match backlog; a balance that never clears means a
    supplier invoice is missing."""
    return tool_response(await container.reports.unbilled_receipts())


# ---------------------------------------------------------------------------
# Audit & ad-hoc query
# ---------------------------------------------------------------------------


@mcp.tool()
@_guard
async def get_audit_log(
    action: str | None = None,
    actor: str | None = None,
    object_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """The append-only record of every mutation, newest first.
    Use for "what changed today?", "who posted BILL-0002?"."""
    return tool_response(await container.audit.list(action, actor, object_id, limit, offset))


@mcp.tool()
@_guard
async def run_query(sql: str, limit: int = 200) -> str:
    """Run a read-only SELECT against the database for questions no other tool
    answers. Writes are rejected twice over: by statement inspection and by the
    read-only connection itself."""
    return tool_response(await container.queries.select(sql, limit))


def main() -> None:
    """Run over stdio (Claude Desktop/Code) or an HTTP transport (containers)."""
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    host = os.getenv("MCP_HOST", "127.0.0.1")
    if transport == "streamable-http":
        mcp.run(transport="streamable-http", host=host, port=MCP_PORT)
    elif transport == "sse":
        mcp.run(transport="sse", host=host, port=MCP_PORT)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
