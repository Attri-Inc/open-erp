-- OpenERP schema — operations (buy, hold, sell) that post to a double-entry GL.
--
-- Units: money is INTEGER minor units (cents); quantity is INTEGER milli-units
-- (1000 = 1.000 unit). There is no float arithmetic anywhere in the project.
--
-- NOTE: "every journal entry is balanced (sum debits == sum credits) and has
-- >= 2 lines" is an APPLICATION invariant enforced in PostingService.post,
-- not a DB constraint — SQLite cannot express it as a CHECK. Documents are
-- immutable once posted; corrections are reversing entries, never edits.

-- ---------------------------------------------------------------------------
-- Masters
-- ---------------------------------------------------------------------------

CREATE TABLE partners (
  id                 TEXT PRIMARY KEY,
  code               TEXT NOT NULL UNIQUE,
  name               TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kind               TEXT NOT NULL CHECK (kind IN ('customer','supplier','both')),
  email              TEXT,
  phone              TEXT,
  payment_terms_days INTEGER NOT NULL DEFAULT 30 CHECK (payment_terms_days >= 0),
  is_archived        INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX idx_partners_kind ON partners(kind);

CREATE TABLE items (
  id               TEXT PRIMARY KEY,
  sku              TEXT NOT NULL UNIQUE,
  name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kind             TEXT NOT NULL CHECK (kind IN ('stockable','service')),
  uom              TEXT NOT NULL DEFAULT 'unit',
  sale_price_minor INTEGER NOT NULL DEFAULT 0 CHECK (sale_price_minor >= 0),
  -- AVCO valuation state, maintained by InventoryService inside the write path.
  on_hand_milli    INTEGER NOT NULL DEFAULT 0,
  value_minor      INTEGER NOT NULL DEFAULT 0,
  is_archived      INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  -- A service is never stocked; keeping it out of valuation is a data rule.
  CHECK (kind = 'stockable' OR (on_hand_milli = 0 AND value_minor = 0))
);
CREATE INDEX idx_items_kind ON items(kind);

CREATE TABLE warehouses (
  id          TEXT PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  is_archived INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- General ledger
-- ---------------------------------------------------------------------------

CREATE TABLE accounts (
  id          TEXT PRIMARY KEY,
  code        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  type        TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
  normal_side TEXT NOT NULL CHECK (normal_side IN ('debit','credit')),
  role        TEXT UNIQUE,   -- bank, ar, ap, inventory, grni, cogs, revenue, ...
  description TEXT,
  is_archived INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX idx_accounts_type ON accounts(type);

CREATE TABLE journal_entries (
  id             TEXT PRIMARY KEY,
  entry_number   TEXT NOT NULL UNIQUE,
  entry_date     TEXT NOT NULL,                -- ISO date (YYYY-MM-DD)
  description    TEXT NOT NULL,
  reference      TEXT,
  status         TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
  reverses_id    TEXT UNIQUE REFERENCES journal_entries(id),
  reversed_by_id TEXT REFERENCES journal_entries(id),
  -- Drill-through: every posting names the document that caused it.
  source_type    TEXT NOT NULL CHECK (source_type IN
                   ('goods_receipt','vendor_bill','supplier_payment','delivery',
                    'customer_invoice','customer_payment','adjustment','opening','reversal')),
  source_id      TEXT,
  created_at     TEXT NOT NULL,
  created_by     TEXT NOT NULL,
  CHECK (reverses_id IS NULL OR reverses_id != id)
);
CREATE INDEX idx_je_date ON journal_entries(entry_date);
CREATE INDEX idx_je_source ON journal_entries(source_type, source_id);

CREATE TABLE journal_lines (
  id           TEXT PRIMARY KEY,
  entry_id     TEXT NOT NULL REFERENCES journal_entries(id) ON DELETE RESTRICT,
  account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  line_no      INTEGER NOT NULL,
  direction    TEXT NOT NULL CHECK (direction IN ('debit','credit')),
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  memo         TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (entry_id, line_no)
);
CREATE INDEX idx_jl_account ON journal_lines(account_id, entry_id);
CREATE INDEX idx_jl_entry ON journal_lines(entry_id);

-- ---------------------------------------------------------------------------
-- Inventory
-- ---------------------------------------------------------------------------

CREATE TABLE stock_moves (
  id               TEXT PRIMARY KEY,
  item_id          TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  warehouse_id     TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  direction        TEXT NOT NULL CHECK (direction IN ('in','out')),
  qty_milli        INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_cost_minor  INTEGER NOT NULL CHECK (unit_cost_minor >= 0),
  value_minor      INTEGER NOT NULL CHECK (value_minor >= 0),
  reason           TEXT NOT NULL CHECK (reason IN ('receipt','delivery','adjustment','opening')),
  source_type      TEXT,
  source_id        TEXT,
  moved_at         TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  created_by       TEXT NOT NULL
);
CREATE INDEX idx_moves_item ON stock_moves(item_id, warehouse_id);
CREATE INDEX idx_moves_date ON stock_moves(moved_at);

-- ---------------------------------------------------------------------------
-- Procure-to-pay
-- ---------------------------------------------------------------------------

CREATE TABLE purchase_orders (
  id           TEXT PRIMARY KEY,
  po_number    TEXT NOT NULL UNIQUE,
  supplier_id  TEXT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
  warehouse_id TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  order_date   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','confirmed','received','billed','cancelled')),
  total_minor  INTEGER NOT NULL DEFAULT 0 CHECK (total_minor >= 0),
  note         TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  created_by   TEXT NOT NULL
);
CREATE INDEX idx_po_supplier ON purchase_orders(supplier_id);
CREATE INDEX idx_po_status ON purchase_orders(status);

CREATE TABLE purchase_order_lines (
  id                 TEXT PRIMARY KEY,
  po_id              TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  line_no            INTEGER NOT NULL,
  item_id            TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli          INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_price_minor   INTEGER NOT NULL CHECK (unit_price_minor >= 0),
  line_total_minor   INTEGER NOT NULL CHECK (line_total_minor >= 0),
  qty_received_milli INTEGER NOT NULL DEFAULT 0 CHECK (qty_received_milli >= 0),
  qty_billed_milli   INTEGER NOT NULL DEFAULT 0 CHECK (qty_billed_milli >= 0),
  UNIQUE (po_id, line_no)
);
CREATE INDEX idx_pol_po ON purchase_order_lines(po_id);

CREATE TABLE goods_receipts (
  id           TEXT PRIMARY KEY,
  grn_number   TEXT NOT NULL UNIQUE,
  po_id        TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE RESTRICT,
  warehouse_id TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  receipt_date TEXT NOT NULL,
  value_minor  INTEGER NOT NULL DEFAULT 0 CHECK (value_minor >= 0),
  entry_id     TEXT REFERENCES journal_entries(id),
  created_at   TEXT NOT NULL,
  created_by   TEXT NOT NULL
);
CREATE INDEX idx_grn_po ON goods_receipts(po_id);

CREATE TABLE goods_receipt_lines (
  id              TEXT PRIMARY KEY,
  grn_id          TEXT NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
  line_no         INTEGER NOT NULL,
  po_line_id      TEXT NOT NULL REFERENCES purchase_order_lines(id) ON DELETE RESTRICT,
  item_id         TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli       INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_cost_minor INTEGER NOT NULL CHECK (unit_cost_minor >= 0),
  value_minor     INTEGER NOT NULL CHECK (value_minor >= 0),
  UNIQUE (grn_id, line_no)
);

CREATE TABLE vendor_bills (
  id                TEXT PRIMARY KEY,
  bill_number       TEXT NOT NULL UNIQUE,
  supplier_ref      TEXT,
  supplier_id       TEXT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
  po_id             TEXT NOT NULL REFERENCES purchase_orders(id) ON DELETE RESTRICT,
  bill_date         TEXT NOT NULL,
  due_date          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'posted'
                      CHECK (status IN ('posted','paid','cancelled')),
  total_minor       INTEGER NOT NULL DEFAULT 0 CHECK (total_minor >= 0),
  amount_paid_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
  entry_id          TEXT REFERENCES journal_entries(id),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  created_by        TEXT NOT NULL
);
CREATE INDEX idx_bills_supplier ON vendor_bills(supplier_id);
CREATE INDEX idx_bills_status ON vendor_bills(status);

CREATE TABLE vendor_bill_lines (
  id               TEXT PRIMARY KEY,
  bill_id          TEXT NOT NULL REFERENCES vendor_bills(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,
  po_line_id       TEXT NOT NULL REFERENCES purchase_order_lines(id) ON DELETE RESTRICT,
  item_id          TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli        INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
  line_total_minor INTEGER NOT NULL CHECK (line_total_minor >= 0),
  UNIQUE (bill_id, line_no)
);

-- ---------------------------------------------------------------------------
-- Order-to-cash
-- ---------------------------------------------------------------------------

CREATE TABLE sales_orders (
  id           TEXT PRIMARY KEY,
  so_number    TEXT NOT NULL UNIQUE,
  customer_id  TEXT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
  warehouse_id TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  order_date   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','confirmed','delivered','invoiced','cancelled')),
  total_minor  INTEGER NOT NULL DEFAULT 0 CHECK (total_minor >= 0),
  note         TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  created_by   TEXT NOT NULL
);
CREATE INDEX idx_so_customer ON sales_orders(customer_id);
CREATE INDEX idx_so_status ON sales_orders(status);

CREATE TABLE sales_order_lines (
  id                  TEXT PRIMARY KEY,
  so_id               TEXT NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
  line_no             INTEGER NOT NULL,
  item_id             TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli           INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_price_minor    INTEGER NOT NULL CHECK (unit_price_minor >= 0),
  line_total_minor    INTEGER NOT NULL CHECK (line_total_minor >= 0),
  qty_delivered_milli INTEGER NOT NULL DEFAULT 0 CHECK (qty_delivered_milli >= 0),
  qty_invoiced_milli  INTEGER NOT NULL DEFAULT 0 CHECK (qty_invoiced_milli >= 0),
  UNIQUE (so_id, line_no)
);
CREATE INDEX idx_sol_so ON sales_order_lines(so_id);

CREATE TABLE deliveries (
  id            TEXT PRIMARY KEY,
  dn_number     TEXT NOT NULL UNIQUE,
  so_id         TEXT NOT NULL REFERENCES sales_orders(id) ON DELETE RESTRICT,
  warehouse_id  TEXT NOT NULL REFERENCES warehouses(id) ON DELETE RESTRICT,
  delivery_date TEXT NOT NULL,
  cost_minor    INTEGER NOT NULL DEFAULT 0 CHECK (cost_minor >= 0),
  entry_id      TEXT REFERENCES journal_entries(id),
  created_at    TEXT NOT NULL,
  created_by    TEXT NOT NULL
);
CREATE INDEX idx_dn_so ON deliveries(so_id);

CREATE TABLE delivery_lines (
  id              TEXT PRIMARY KEY,
  delivery_id     TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
  line_no         INTEGER NOT NULL,
  so_line_id      TEXT NOT NULL REFERENCES sales_order_lines(id) ON DELETE RESTRICT,
  item_id         TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli       INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_cost_minor INTEGER NOT NULL CHECK (unit_cost_minor >= 0),
  value_minor     INTEGER NOT NULL CHECK (value_minor >= 0),
  UNIQUE (delivery_id, line_no)
);

CREATE TABLE customer_invoices (
  id                TEXT PRIMARY KEY,
  invoice_number    TEXT NOT NULL UNIQUE,
  customer_id       TEXT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
  so_id             TEXT NOT NULL REFERENCES sales_orders(id) ON DELETE RESTRICT,
  invoice_date      TEXT NOT NULL,
  due_date          TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'posted'
                      CHECK (status IN ('posted','paid','cancelled')),
  total_minor       INTEGER NOT NULL DEFAULT 0 CHECK (total_minor >= 0),
  amount_paid_minor INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_minor >= 0),
  entry_id          TEXT REFERENCES journal_entries(id),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  created_by        TEXT NOT NULL
);
CREATE INDEX idx_inv_customer ON customer_invoices(customer_id);
CREATE INDEX idx_inv_status ON customer_invoices(status);

CREATE TABLE customer_invoice_lines (
  id               TEXT PRIMARY KEY,
  invoice_id       TEXT NOT NULL REFERENCES customer_invoices(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,
  so_line_id       TEXT NOT NULL REFERENCES sales_order_lines(id) ON DELETE RESTRICT,
  item_id          TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  qty_milli        INTEGER NOT NULL CHECK (qty_milli > 0),
  unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
  line_total_minor INTEGER NOT NULL CHECK (line_total_minor >= 0),
  UNIQUE (invoice_id, line_no)
);

CREATE TABLE payments (
  id             TEXT PRIMARY KEY,
  payment_number TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL CHECK (kind IN ('receipt','disbursement')),
  partner_id     TEXT NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,
  document_type  TEXT NOT NULL CHECK (document_type IN ('vendor_bill','customer_invoice')),
  document_id    TEXT NOT NULL,
  amount_minor   INTEGER NOT NULL CHECK (amount_minor > 0),
  payment_date   TEXT NOT NULL,
  reference      TEXT,
  entry_id       TEXT REFERENCES journal_entries(id),
  created_at     TEXT NOT NULL,
  created_by     TEXT NOT NULL
);
CREATE INDEX idx_pay_document ON payments(document_type, document_id);

-- ---------------------------------------------------------------------------
-- Cross-cutting
-- ---------------------------------------------------------------------------

CREATE TABLE sequences (
  name       TEXT PRIMARY KEY,
  prefix     TEXT NOT NULL,
  padding    INTEGER NOT NULL DEFAULT 4,
  next_value INTEGER NOT NULL DEFAULT 1 CHECK (next_value > 0)
);

CREATE TABLE audit_log (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  id          TEXT NOT NULL UNIQUE,
  actor       TEXT NOT NULL,
  action      TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id   TEXT,
  details     TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_object ON audit_log(object_type, object_id);

CREATE TABLE org_settings (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  business_name TEXT NOT NULL,
  base_currency TEXT NOT NULL DEFAULT 'USD',
  price_tolerance_minor INTEGER NOT NULL DEFAULT 0 CHECK (price_tolerance_minor >= 0),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
