# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-07

Initial release.

### Added
- SQLite schema for masters, procure-to-pay, order-to-cash, stock moves and a
  double-entry general ledger, with an append-only audit log.
- `PostingService` — the single write path into the ledger, enforcing balanced,
  minimum-two-line, positive-amount, immutable entries.
- Procure-to-pay: purchase orders, partial goods receipts, vendor bills gated by
  three-way matching, and payments.
- Order-to-cash: sales orders, partial deliveries costed at AVCO, customer
  invoices limited to what shipped, and cash application.
- Inventory: immutable stock moves with an average-cost valuation pool that
  moves in the same statement, negative-stock refusal, and GL-posted adjustments.
- Reports: trial balance, profit & loss, balance sheet, AR and AP aging, and the
  GRNI unbilled-receipts backlog.
- MCP server with 47 tools over stdio, SSE and streamable-http transports.
- Seed script that builds a running business through the real services, and a
  smoke test that exercises the full tool surface.
- 37 tests covering the ledger, valuation, matching and rollback invariants.

### Notes
- Balance queries intentionally include reversed entries; a reversal is a contra
  posting and both sides must count, or every account balance is wrong while the
  trial balance still reports "balanced".
- Built against `mcp` 2.x (`MCPServer`), not the 1.x `FastMCP` API.
