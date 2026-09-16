# Connecting OpenERP to Claude

OpenERP speaks MCP over three transports. Use **stdio** for Claude Desktop and
Claude Code on the same machine; use **streamable-http** when the server runs in
a container or on another host.

---

## Claude Code

```bash
claude mcp add openerp -s user -- \
  /absolute/path/to/open-erp/.venv/bin/python \
  /absolute/path/to/open-erp/run_mcp.py
```

Absolute paths matter: `run_mcp.py` chdirs to the project root so the default
database path (`./data/openerp.db`) resolves regardless of where Claude Code was
launched.

Verify:

```bash
claude mcp list
```

Then ask Claude something the tools can answer — *"are our books balanced?"* or
*"what have we received that nobody has billed us for?"*

---

## Claude Desktop

Edit the config file:

- macOS `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "openerp": {
      "command": "/absolute/path/to/open-erp/.venv/bin/python",
      "args": ["/absolute/path/to/open-erp/run_mcp.py"],
      "env": {
        "OPENERP_DB": "/absolute/path/to/open-erp/data/openerp.db"
      }
    }
  }
}
```

Restart Claude Desktop. The tools appear under the connectors menu.

---

## HTTP transport (containers, remote hosts)

```bash
docker compose up --build
```

The compose file publishes to `127.0.0.1:8793` only. To reach it from another
machine you must both change that binding *and* put authentication in front of
it — OpenERP has no auth of its own in v0.1, and anyone who can reach the port
can post to the books.

To run the HTTP transport directly:

```bash
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8793 python -m src.mcp_server
```

`MCP_TRANSPORT=sse` is also supported for older clients.

---

## First questions to try

Once connected, these exercise most of the surface:

| Ask | Tools it reaches |
|---|---|
| "Are our books balanced?" | `get_trial_balance` |
| "What's in the warehouse and what's it worth?" | `get_stock_on_hand`, `get_stock_valuation` |
| "Who owes us money, and how late are they?" | `get_ar_aging` |
| "What have we received that nobody has billed us for?" | `get_unbilled_receipts` |
| "Order 100 more of SKU-200 from Northwind at $6.80" | `create_purchase_order`, `confirm_purchase_order` |
| "Northwind's invoice says $9.00 a unit — does that match?" | `match_vendor_bill` |
| "Ship 20 hinges to Meridian and invoice them" | `deliver_goods`, `post_customer_invoice` |
| "Why did SKU-100 stock change last week?" | `list_stock_moves` |
| "Who posted BILL-0002?" | `get_audit_log` |

---

## Troubleshooting

**Tools do not appear.** Run `python run_mcp.py` by hand — an import error or a
missing database shows up immediately there and is swallowed by the client.

**"No account is mapped to the 'inventory' role."** The database has no chart of
accounts. Run `python scripts/seed.py`, or map the roles yourself with
`create_account(code, name, account_type, role=...)`.

**Empty results everywhere.** The server is pointed at a different database than
you seeded. Check `OPENERP_DB` in the client config and in your shell.

**The books do not balance.** That should be impossible through the tools. Run
`python scripts/smoke_test.py`; if it also reports an imbalance, something wrote
around the posting service — treat it as a data-integrity incident and open an
issue with the `get_trial_balance` output.
