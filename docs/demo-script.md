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

| # | Prompt | What lands on screen |
|---|--------|----------------------|
| 1 | *Using OpenERP, are our books balanced?* | `get_trial_balance` — debits equal credits |
| 2 | *Raise a purchase order with Northwind Components for 100 aluminium brackets at $4.50 each into the main warehouse, and confirm it.* | `create_purchase_order` + `confirm_purchase_order` — PO-0007, $450.00 |
| 3 | *Only 90 turned up. Book the goods receipt.* | `receive_goods` — GRN-0007, 90 units, $405.00 to inventory |
| 4 | *Northwind just billed us for all 100. Can we post that bill?* | `match_vendor_bill` — **blocked**: "billed 100, but only 90 is received and not yet billed" |
| 5 | *What have we received that nobody has billed us for?* | `get_unbilled_receipts` — the GRNI balance |

Step 4 is the point of the demo: the bill is refused by the data, not by a
reviewer remembering to check.

## Verified output

Driven over the MCP stdio transport against a seeded database:

```
PO-0007   total $450.00   status confirmed
GRN-0007  90 units        value $405.00
match     matched=false   line 1: billed 100, but only 90 is received and not yet billed
trial balance  $12,670.00 debit / $12,670.00 credit   balanced=true
```

## Capture settings

Match `open-ledger`'s demo: 1280x832, GIF, under ~600KB. Trim to the tool calls
and their results — the idle typing between prompts can go.
