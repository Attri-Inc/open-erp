# Recording the Cowork demo

The README's **See it in action** GIF is `docs/images/mcp-demo.gif`, rendered from
the transcript this script produces. Recording the same session in Claude Cowork
and saving it over that file swaps in a screen capture. This is the script — a procure-to-pay cycle that ends with three-way matching
refusing a bill nobody should pay.

## Before recording

The connector must be registered in
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "openerp": {
      "command": "/absolute/path/to/open-erp/.venv/bin/python",
      "args": ["/absolute/path/to/open-erp/run_mcp.py"],
      "env": { "OPENERP_DB": "/absolute/path/to/open-erp/data/openerp.db" }
    }
  }
}
```

Reseed first so the numbers below reproduce exactly:

```bash
python scripts/seed.py
```

Restart Claude Desktop, open **Cowork**, and confirm `openerp` is listed under
connectors.

## The prompts

Typed the way someone actually talks to an assistant — lowercase, short, no
nouns spelled out. The first three interrogate data that is already seeded; the
last three create one order so the three-way match has something to refuse.

| # | Prompt | What lands on screen |
|---|--------|----------------------|
| 1 | *what's in the warehouse?* | `get_stock_on_hand` — 53 brackets, 560 hinges, 700 cartons, all at MAIN |
| 2 | *what's our oldest unpaid invoice?* | `get_ar_aging` — INV-0002, Meridian Retail, due 2026-08-24, 23 days overdue, $1,230.00 |
| 3 | *how much stock do we have?* | `get_stock_valuation` — $954.00 + $3,670.50 + $665.00 = **$5,289.50** at AVCO |
| 4 | *northwind are sending 100 brackets at $4.50, set it up* | `create_purchase_order` — PO-0004, $450.00 |
| 5 | *only 90 turned up* | `receive_goods` — GRN-0004, 90 of 100, $405.00 |
| 6 | *they've invoiced us for all 100 though, post it* | `match_vendor_bill` — **blocked**: "billed 100, but only 90 is received and not yet billed" |

Step 6 is the point of the demo: the bill is refused by the data, not by a
reviewer remembering to check. Good follow-up to hold on — *so what do I do?*

### Other questions the seeded data answers well

Two of these surface real problems already sitting in the books:

- *did we forget to bill anyone?* — SO-0003, Rivet & Co, shipped DN-0003 on
  2026-09-13, **$850.00 never invoiced** while $450.00 of COGS was booked.
- *anything we've received but not paid for?* — **$1,700.00** in GRNI; PO-0003
  from Northwind, received 2026-09-10, no vendor bill exists.
- *why do we only have 53 brackets?* — audit row 45,
  "-2 unit of SKU-100 at MAIN · Damaged in handling".
- *what's our best margin?* — shipping cartons, 56.8% ($0.95 → $2.20).
- *can I just delete that entry?* — no delete exists among the 47 tools;
  corrections are contra postings, and `run_query` refuses anything but SELECT.

## Verified output

Driven over the MCP stdio transport against a freshly seeded database:

```
stock on hand   53 SKU-100 · 560 SKU-200 · 700 SKU-300, all at MAIN
oldest unpaid   INV-0002 Meridian Retail, due 2026-08-24, 23 days, $1,230.00
stock value     $5,289.50 at AVCO
PO-0004         total $450.00   confirmed
GRN-0004        90 of 100       value $405.00
match           matched=false   billed 100, but only 90 is received and not yet billed
```

## Capture settings

Match `open-ledger`'s demo: 1280x832, GIF, under ~600KB. Trim to the tool calls
and their results — the idle typing between prompts can go.
