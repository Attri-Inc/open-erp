# OpenERP Vision

## The problem

ERP software is where a business keeps the record of what it bought, what it
holds, what it sold, and what it is owed. Every mature option — Odoo, ERPNext,
NetSuite, SAP — assumes a person is driving: forms, wizards, approval screens.
An agent pointed at one of those is automating a user interface, not calling a
system of record.

Agent frameworks have the opposite problem. They orchestrate well and remember
nothing durable. There is nowhere to put a purchase order.

OpenERP is the operational system of record for that gap: an ERP whose primary
interface is a tool call, with guarantees the spreadsheet it replaces cannot
offer — every posting balanced, every quantity backed by a value, every
document reversible and traceable to the journal entry it produced.

## What OpenERP is

A **local-first ERP** — purchasing, inventory, sales — exposed through a Model
Context Protocol (MCP) server, so operations are queryable and executable by
humans *and* by agents through the same tools and the same rules.

The data model is deliberately small and classic:

- **masters** — `partners` (customers and suppliers), `items` (stockable or
  service), `warehouses`, and the `accounts` chart, where each account may claim
  a posting **role** (`bank`, `ar`, `ap`, `inventory`, `grni`, `cogs`,
  `revenue`, `expense`, `equity`).
- **procure-to-pay** — `purchase_orders` → `goods_receipts` → `vendor_bills` →
  `payments`, with three-way matching between them.
- **order-to-cash** — `sales_orders` → `deliveries` → `customer_invoices` →
  `payments`.
- **`stock_moves`** — immutable movements, each carrying the cost at which it
  moved, keeping quantity and value in step.
- **`journal_entries` + `journal_lines`** — immutable, balanced postings. Every
  entry names the document that caused it, so any figure drills through.
- **`audit_log`** — an append-only record of every mutation, written in the same
  database transaction as the change it describes.

## Relationship to OpenLedger

[open-ledger](https://github.com/Attri-Inc/open-ledger) is the family's
general-purpose set of books. OpenERP does not replace it — it is the layer
above: the documents and stock that *generate* postings. OpenERP ships with its
own posting kernel so it runs standalone with zero dependencies, but the kernel
sits behind one interface (`PostingService`) precisely so a future adapter can
send the same entries to an OpenLedger instance instead. Operations here, books
there, when a deployment wants them separate.

## Principles

1. **Correct by construction.** A journal entry cannot be saved unless it has at
   least two lines and its debits equal its credits. Amounts are integer minor
   units, quantities integer milli-units — there is no float arithmetic
   anywhere in the project.
2. **Immutable, not editable.** Postings are never updated or deleted.
   Corrections happen through `reverse_entry`, a contra posting that preserves
   the original. The ledger is a ledger.
3. **Controls that block, not warn.** Three-way matching refuses to post a bill
   that disagrees with its purchase order or receipts. Stock refuses to go
   negative. A control that can be clicked past is not a control.
4. **Quantity and value are one thing.** Every stock move shifts the valuation
   pool in the same statement. On-hand and stock value cannot drift apart, and
   the test suite asserts stock valuation equals the Inventory account.
5. **Agent-callable from day one.** Every action a human can take is an MCP
   tool. Agents and people operate on the same records through the same rules.
6. **Auditable.** Every mutation writes an audit row atomically with the change,
   and every journal line names its source document.
7. **Local-first.** Ships as SQLite + a single Python process. Self-hosted, no
   data leaves the operator's machine.

## What OpenERP is *not* (v0.1)

- Not multi-currency. One base currency per deployment; no FX conversion.
- Not a tax engine. Line totals are quantity × price; no tax codes or jurisdictions.
- Not manufacturing. No bill of materials, routing, work orders or job cards.
- Not multi-tenant. Each deployment is a single company's books.
- Not fiscal periods. Any date is postable; there is no period close or lock.
- Not FIFO. Costing is average cost only, though the seam for FIFO is in place.

## Roadmap

### v0.1 (current)
MCP server (47 tools) over a SQLite operational database: master data,
procure-to-pay with enforced three-way matching, order-to-cash, AVCO inventory
valuation posted in real time, and the reports that fall out of a correct
ledger — trial balance, P&L, balance sheet, AR/AP aging, and the GRNI backlog.

### v0.2
- REST API twin of every MCP tool (FastAPI, port 8792).
- API-key auth with read / write / admin roles; audit actor from the
  authenticated principal.
- Tax codes on order and invoice lines, with a tax liability account role.
- Optimistic locking (`version`) on mutable rows; idempotency keys on writes.

### v0.3
- Fiscal periods with `OPEN` / `CLOSED` / `LOCKED` status gating postings.
- FIFO as a second costing strategy behind the existing `record_in` /
  `record_out` seam.
- Credit notes and vendor debit notes as first-class reversing documents.
- Hash-chained, tamper-evident audit log with a `verify-audit-chain` command.

### v1.0
- Postgres backend option behind the same repository contracts, with the
  balance rule promoted to a deferrable constraint trigger.
- OpenLedger adapter: post to an external set of books instead of the internal
  kernel.
- Manufacturing: bill of materials, work orders, and WIP valuation.
- Stable schema commitment.
