"""SQLite implementations of the repository protocols.

This is the only module in the project that writes SQL. Services never see a
query; swapping SQLite for Postgres means reimplementing this file and
`container.py`, and nothing else.

Querying is raw parameterized SQL over aiosqlite — no ORM. Every value is
bound, never interpolated, except in `SqliteQueryRepository`, which is guarded
separately.
"""

import re
from typing import Any

import aiosqlite

from src.domain.errors import ConflictError, ValidationError
from src.infrastructure.database import Database, fetch_all


def _dicts(rows: list[aiosqlite.Row]) -> list[dict]:
    return [dict(row) for row in rows]


class _Repository:
    """Shared read plumbing: every repository reads through the read-only
    connection, so a repository can never accidentally mutate outside a
    Unit of Work."""

    def __init__(self, db: Database):
        self._db = db

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        conn = await self._db.readonly()
        return _dicts(await fetch_all(conn, sql, params))

    async def _one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def _count(self, sql: str, params: tuple | list = ()) -> int:
        row = await self._one(sql, params)
        return int(next(iter(row.values()))) if row else 0


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

_PARTNER_COLUMNS = (
    "id, code, name, kind, email, phone, payment_terms_days, is_archived, created_at, updated_at"
)


class SqlitePartnerRepository(_Repository):
    async def get(self, identifier: str) -> dict | None:
        return await self._one(
            f"SELECT {_PARTNER_COLUMNS} FROM partners "
            "WHERE id = ? OR code = ? OR name = ? COLLATE NOCASE LIMIT 1",
            (identifier, identifier, identifier),
        )

    async def list_rows(
        self, kind: str | None, include_archived: bool, q: str | None
    ) -> list[dict]:
        sql = f"SELECT {_PARTNER_COLUMNS} FROM partners WHERE 1=1"
        params: list[Any] = []
        if kind:
            # 'both' partners answer to either role, so a customer filter must include them.
            sql += " AND kind IN (?, 'both')"
            params.append(kind)
        if not include_archived:
            sql += " AND is_archived = 0"
        if q:
            sql += " AND (name LIKE ? COLLATE NOCASE OR code LIKE ? COLLATE NOCASE)"
            params += [f"%{q}%", f"%{q}%"]
        return await self._all(sql + " ORDER BY code", params)

    async def insert(self, conn: Any, partner: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO partners (id, code, name, kind, email, phone, "
                "payment_terms_days, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    partner["id"],
                    partner["code"],
                    partner["name"],
                    partner["kind"],
                    partner["email"],
                    partner["phone"],
                    partner["payment_terms_days"],
                    partner["created_at"],
                    partner["updated_at"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                f"Partner '{partner['code']}' or name '{partner['name']}' already exists"
            ) from exc


_ITEM_COLUMNS = (
    "id, sku, name, kind, uom, sale_price_minor, on_hand_milli, value_minor, "
    "is_archived, created_at, updated_at"
)


class SqliteItemRepository(_Repository):
    async def get(self, identifier: str) -> dict | None:
        return await self._one(
            f"SELECT {_ITEM_COLUMNS} FROM items "
            "WHERE id = ? OR sku = ? OR name = ? COLLATE NOCASE LIMIT 1",
            (identifier, identifier, identifier),
        )

    async def list_rows(
        self, kind: str | None, include_archived: bool, q: str | None
    ) -> list[dict]:
        sql = f"SELECT {_ITEM_COLUMNS} FROM items WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if not include_archived:
            sql += " AND is_archived = 0"
        if q:
            sql += " AND (name LIKE ? COLLATE NOCASE OR sku LIKE ? COLLATE NOCASE)"
            params += [f"%{q}%", f"%{q}%"]
        return await self._all(sql + " ORDER BY sku", params)

    async def on_hand_by_warehouse(self, item_id: str | None) -> list[dict]:
        sql = """
            SELECT i.id AS item_id, i.sku, i.name, i.uom, w.id AS warehouse_id,
                   w.code AS warehouse_code, w.name AS warehouse_name,
                   SUM(CASE WHEN m.direction = 'in' THEN m.qty_milli ELSE -m.qty_milli END)
                       AS qty_milli
            FROM stock_moves m
            JOIN items i ON i.id = m.item_id
            JOIN warehouses w ON w.id = m.warehouse_id
        """
        params: list[Any] = []
        if item_id:
            sql += " WHERE m.item_id = ?"
            params.append(item_id)
        sql += " GROUP BY i.id, w.id HAVING qty_milli != 0 ORDER BY i.sku, w.code"
        return await self._all(sql, params)

    async def insert(self, conn: Any, item: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO items (id, sku, name, kind, uom, sale_price_minor, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item["sku"],
                    item["name"],
                    item["kind"],
                    item["uom"],
                    item["sale_price_minor"],
                    item["created_at"],
                    item["updated_at"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                f"Item '{item['sku']}' or name '{item['name']}' already exists"
            ) from exc

    async def apply_valuation(
        self, conn: Any, item_id: str, qty_delta_milli: int, value_delta_minor: int, now: str
    ) -> None:
        """Move the AVCO pool. Quantity and value always move together, in the
        same statement, so on-hand and stock value can never drift apart."""
        await conn.execute(
            "UPDATE items SET on_hand_milli = on_hand_milli + ?, "
            "value_minor = value_minor + ?, updated_at = ? WHERE id = ?",
            (qty_delta_milli, value_delta_minor, now, item_id),
        )


class SqliteWarehouseRepository(_Repository):
    async def get(self, identifier: str) -> dict | None:
        return await self._one(
            "SELECT id, code, name, is_archived, created_at, updated_at FROM warehouses "
            "WHERE id = ? OR code = ? OR name = ? COLLATE NOCASE LIMIT 1",
            (identifier, identifier, identifier),
        )

    async def list_rows(self, include_archived: bool) -> list[dict]:
        sql = "SELECT id, code, name, is_archived, created_at, updated_at FROM warehouses"
        if not include_archived:
            sql += " WHERE is_archived = 0"
        return await self._all(sql + " ORDER BY code")

    async def insert(self, conn: Any, warehouse: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO warehouses (id, code, name, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (
                    warehouse["id"],
                    warehouse["code"],
                    warehouse["name"],
                    warehouse["created_at"],
                    warehouse["updated_at"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(f"Warehouse '{warehouse['code']}' already exists") from exc


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


class SqliteStockRepository(_Repository):
    async def moves(
        self,
        item_id: str | None,
        warehouse_id: str | None,
        date_from: str | None,
        date_to: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if item_id:
            where += " AND m.item_id = ?"
            params.append(item_id)
        if warehouse_id:
            where += " AND m.warehouse_id = ?"
            params.append(warehouse_id)
        if date_from:
            where += " AND m.moved_at >= ?"
            params.append(date_from)
        if date_to:
            where += " AND m.moved_at <= ?"
            params.append(date_to)
        total = await self._count(f"SELECT COUNT(*) FROM stock_moves m{where}", params)
        rows = await self._all(
            "SELECT m.id, m.direction, m.qty_milli, m.unit_cost_minor, m.value_minor, "
            "m.reason, m.source_type, m.source_id, m.moved_at, i.sku, i.name AS item_name, "
            "w.code AS warehouse_code FROM stock_moves m "
            "JOIN items i ON i.id = m.item_id JOIN warehouses w ON w.id = m.warehouse_id"
            f"{where} ORDER BY m.moved_at DESC, m.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def on_hand(self, item_id: str, warehouse_id: str) -> int:
        row = await self._one(
            "SELECT COALESCE(SUM(CASE WHEN direction = 'in' THEN qty_milli "
            "ELSE -qty_milli END), 0) AS qty FROM stock_moves "
            "WHERE item_id = ? AND warehouse_id = ?",
            (item_id, warehouse_id),
        )
        return int(row["qty"]) if row else 0

    async def insert_move(self, conn: Any, move: dict) -> None:
        await conn.execute(
            "INSERT INTO stock_moves (id, item_id, warehouse_id, direction, qty_milli, "
            "unit_cost_minor, value_minor, reason, source_type, source_id, moved_at, "
            "created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                move["id"],
                move["item_id"],
                move["warehouse_id"],
                move["direction"],
                move["qty_milli"],
                move["unit_cost_minor"],
                move["value_minor"],
                move["reason"],
                move["source_type"],
                move["source_id"],
                move["moved_at"],
                move["created_at"],
                move["created_by"],
            ),
        )


# ---------------------------------------------------------------------------
# General ledger
# ---------------------------------------------------------------------------

_ACCOUNT_COLUMNS = (
    "id, code, name, type, normal_side, role, description, is_archived, created_at, updated_at"
)


class SqliteAccountRepository(_Repository):
    """Balance queries deliberately count reversed entries.

    A reversal is a contra posting, not a deletion: the original and its contra
    both stay in the ledger and net to zero. Excluding `status = 'reversed'`
    here would subtract the reversal a second time and quietly corrupt every
    account balance while the trial balance still reported "balanced".
    """

    async def get(self, identifier: str) -> dict | None:
        return await self._one(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts "
            "WHERE id = ? OR code = ? OR name = ? COLLATE NOCASE LIMIT 1",
            (identifier, identifier, identifier),
        )

    async def get_by_role(self, role: str) -> dict | None:
        return await self._one(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE role = ? LIMIT 1", (role,)
        )

    async def list_rows(self, account_type: str | None, include_archived: bool) -> list[dict]:
        sql = f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE 1=1"
        params: list[Any] = []
        if account_type:
            sql += " AND type = ?"
            params.append(account_type)
        if not include_archived:
            sql += " AND is_archived = 0"
        return await self._all(sql + " ORDER BY code", params)

    async def net_debit(self, account_id: str, as_of: str | None) -> int:
        sql = (
            "SELECT COALESCE(SUM(CASE WHEN l.direction = 'debit' THEN l.amount_minor "
            "ELSE -l.amount_minor END), 0) AS net FROM journal_lines l "
            "JOIN journal_entries e ON e.id = l.entry_id "
            "WHERE l.account_id = ?"
        )
        params: list[Any] = [account_id]
        if as_of:
            sql += " AND e.entry_date <= ?"
            params.append(as_of)
        row = await self._one(sql, params)
        return int(row["net"]) if row else 0

    async def trial_balance_rows(self, as_of: str | None) -> list[dict]:
        sql = (
            "SELECT a.id, a.code, a.name, a.type, a.normal_side, "
            "COALESCE(SUM(CASE WHEN l.direction = 'debit' THEN l.amount_minor "
            "ELSE -l.amount_minor END), 0) AS net_debit_minor "
            "FROM accounts a "
            "LEFT JOIN journal_lines l ON l.account_id = a.id "
            "LEFT JOIN journal_entries e ON e.id = l.entry_id "
        )
        params: list[Any] = []
        if as_of:
            sql += "AND e.entry_date <= ? "
            params.append(as_of)
        # The join to entries is filtered above; a line whose entry was excluded
        # contributes NULL and must not be summed, hence the e.id guard.
        sql = sql.replace(
            "COALESCE(SUM(CASE WHEN l.direction",
            "COALESCE(SUM(CASE WHEN e.id IS NULL THEN 0 WHEN l.direction",
        )
        sql += "GROUP BY a.id ORDER BY a.code"
        return await self._all(sql, params)

    async def type_totals(
        self, account_type: str, date_from: str | None, date_to: str | None
    ) -> list[dict]:
        sql = (
            "SELECT a.id, a.code, a.name, a.normal_side, "
            "COALESCE(SUM(CASE WHEN e.id IS NULL THEN 0 WHEN l.direction = 'debit' "
            "THEN l.amount_minor ELSE -l.amount_minor END), 0) AS net_debit_minor "
            "FROM accounts a "
            "LEFT JOIN journal_lines l ON l.account_id = a.id "
            "LEFT JOIN journal_entries e ON e.id = l.entry_id "
        )
        params: list[Any] = []
        if date_from:
            sql += "AND e.entry_date >= ? "
            params.append(date_from)
        if date_to:
            sql += "AND e.entry_date <= ? "
            params.append(date_to)
        sql += "WHERE a.type = ? GROUP BY a.id ORDER BY a.code"
        params.append(account_type)
        return await self._all(sql, params)

    async def ledger_rows(self, account_id: str, date_to: str | None) -> list[dict]:
        sql = (
            "SELECT l.id, e.id AS entry_id, e.entry_number, e.entry_date, e.description, "
            "e.source_type, e.source_id, l.direction, l.amount_minor, l.memo "
            "FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id "
            "WHERE l.account_id = ?"
        )
        params: list[Any] = [account_id]
        if date_to:
            sql += " AND e.entry_date <= ?"
            params.append(date_to)
        return await self._all(sql + " ORDER BY e.entry_date, e.entry_number, l.line_no", params)

    async def insert(self, conn: Any, account: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO accounts (id, code, name, type, normal_side, role, description, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    account["id"],
                    account["code"],
                    account["name"],
                    account["type"],
                    account["normal_side"],
                    account.get("role"),
                    account.get("description"),
                    account["created_at"],
                    account["updated_at"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(
                f"Account '{account['code']}' or name '{account['name']}' already exists"
            ) from exc


class SqliteJournalRepository(_Repository):
    async def get(self, entry_id: str) -> dict | None:
        return await self._one(
            "SELECT id, entry_number, entry_date, description, reference, status, "
            "reverses_id, reversed_by_id, source_type, source_id, created_at, created_by "
            "FROM journal_entries WHERE id = ? OR entry_number = ? LIMIT 1",
            (entry_id, entry_id),
        )

    async def lines(self, entry_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.line_no, l.direction, l.amount_minor, l.memo, "
            "a.code AS account_code, a.name AS account_name "
            "FROM journal_lines l JOIN accounts a ON a.id = l.account_id "
            "WHERE l.entry_id = ? ORDER BY l.line_no",
            (entry_id,),
        )

    async def search(
        self,
        q: str | None,
        date_from: str | None,
        date_to: str | None,
        source_type: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if q:
            where += " AND (e.description LIKE ? OR e.reference LIKE ? OR e.entry_number LIKE ?)"
            params += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if date_from:
            where += " AND e.entry_date >= ?"
            params.append(date_from)
        if date_to:
            where += " AND e.entry_date <= ?"
            params.append(date_to)
        if source_type:
            where += " AND e.source_type = ?"
            params.append(source_type)
        total = await self._count(f"SELECT COUNT(*) FROM journal_entries e{where}", params)
        rows = await self._all(
            "SELECT e.id, e.entry_number, e.entry_date, e.description, e.reference, e.status, "
            "e.source_type, e.source_id, "
            "(SELECT COALESCE(SUM(amount_minor), 0) FROM journal_lines "
            " WHERE entry_id = e.id AND direction = 'debit') AS total_minor "
            f"FROM journal_entries e{where} "
            "ORDER BY e.entry_date DESC, e.entry_number DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def by_source(self, source_type: str, source_id: str) -> dict | None:
        return await self._one(
            "SELECT id, entry_number, entry_date, description, status FROM journal_entries "
            "WHERE source_type = ? AND source_id = ? LIMIT 1",
            (source_type, source_id),
        )

    async def insert(self, conn: Any, entry: dict) -> None:
        await conn.execute(
            "INSERT INTO journal_entries (id, entry_number, entry_date, description, reference, "
            "reverses_id, source_type, source_id, created_at, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entry["id"],
                entry["entry_number"],
                entry["entry_date"],
                entry["description"],
                entry.get("reference"),
                entry.get("reverses_id"),
                entry["source_type"],
                entry.get("source_id"),
                entry["created_at"],
                entry["created_by"],
            ),
        )

    async def insert_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO journal_lines (id, entry_id, account_id, line_no, direction, "
            "amount_minor, memo, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                line["id"],
                line["entry_id"],
                line["account_id"],
                line["line_no"],
                line["direction"],
                line["amount_minor"],
                line.get("memo"),
                line["created_at"],
            ),
        )

    async def mark_reversed(self, conn: Any, original_id: str, contra_id: str) -> None:
        await conn.execute(
            "UPDATE journal_entries SET status = 'reversed', reversed_by_id = ? "
            "WHERE id = ? AND reversed_by_id IS NULL",
            (contra_id, original_id),
        )


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class SqliteSequenceRepository(_Repository):
    async def next_number(self, conn: Any, name: str) -> str:
        """Claim the next document number inside the caller's transaction, so a
        rolled-back document never burns a number."""
        cursor = await conn.execute(
            "UPDATE sequences SET next_value = next_value + 1 WHERE name = ? "
            "RETURNING prefix, padding, next_value - 1",
            (name,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValidationError(f"Unknown document sequence '{name}'")
        prefix, padding, value = row[0], int(row[1]), int(row[2])
        return f"{prefix}{value:0{padding}d}"


class SqliteAuditRepository(_Repository):
    async def list(
        self,
        action: str | None,
        actor: str | None,
        object_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if action:
            where += " AND action = ?"
            params.append(action)
        if actor:
            where += " AND actor = ?"
            params.append(actor)
        if object_id:
            where += " AND object_id = ?"
            params.append(object_id)
        total = await self._count(f"SELECT COUNT(*) FROM audit_log{where}", params)
        rows = await self._all(
            "SELECT seq, id, actor, action, object_type, object_id, details, created_at "
            f"FROM audit_log{where} ORDER BY seq DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def append(self, conn: Any, entry: dict) -> None:
        await conn.execute(
            "INSERT INTO audit_log (id, actor, action, object_type, object_id, details, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (
                entry["id"],
                entry["actor"],
                entry["action"],
                entry["object_type"],
                entry.get("object_id"),
                entry.get("details"),
                entry["created_at"],
            ),
        )


class SqliteSettingsRepository(_Repository):
    async def get(self) -> dict | None:
        return await self._one(
            "SELECT business_name, base_currency, price_tolerance_minor FROM org_settings "
            "WHERE id = 1"
        )


_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b",
    re.IGNORECASE,
)


class SqliteQueryRepository(_Repository):
    """Ad-hoc read-only SQL for agents.

    Two independent guards: the statement is rejected unless it is a single
    SELECT with no mutating keyword, and it runs on the `query_only` connection,
    which the engine itself refuses to write through.
    """

    async def select(self, sql: str) -> list[dict]:
        statement = sql.strip().rstrip(";")
        if ";" in statement:
            raise ValidationError("Only a single statement is allowed")
        if not statement.lower().startswith(("select", "with")):
            raise ValidationError("Only SELECT queries are allowed")
        if _FORBIDDEN_SQL.search(statement):
            raise ValidationError("Only read-only SELECT queries are allowed")
        return await self._all(statement)


# ---------------------------------------------------------------------------
# Procure-to-pay
# ---------------------------------------------------------------------------

_PO_COLUMNS = (
    "po.id, po.po_number, po.supplier_id, po.warehouse_id, po.order_date, po.status, "
    "po.total_minor, po.note, po.created_at, po.updated_at, po.created_by"
)


class SqlitePurchaseRepository(_Repository):
    async def get_order(self, identifier: str) -> dict | None:
        return await self._one(
            f"SELECT {_PO_COLUMNS}, p.name AS supplier_name, w.code AS warehouse_code "
            "FROM purchase_orders po "
            "JOIN partners p ON p.id = po.supplier_id "
            "JOIN warehouses w ON w.id = po.warehouse_id "
            "WHERE po.id = ? OR po.po_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def order_lines(self, po_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.id, l.line_no, l.item_id, l.qty_milli, l.unit_price_minor, "
            "l.line_total_minor, l.qty_received_milli, l.qty_billed_milli, "
            "i.sku, i.name AS item_name, i.kind AS item_kind, i.uom "
            "FROM purchase_order_lines l JOIN items i ON i.id = l.item_id "
            "WHERE l.po_id = ? ORDER BY l.line_no",
            (po_id,),
        )

    async def get_line(self, line_id: str) -> dict | None:
        return await self._one(
            "SELECT id, po_id, line_no, item_id, qty_milli, unit_price_minor, "
            "line_total_minor, qty_received_milli, qty_billed_milli "
            "FROM purchase_order_lines WHERE id = ?",
            (line_id,),
        )

    async def search_orders(
        self, supplier_id: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if supplier_id:
            where += " AND po.supplier_id = ?"
            params.append(supplier_id)
        if status:
            where += " AND po.status = ?"
            params.append(status)
        total = await self._count(f"SELECT COUNT(*) FROM purchase_orders po{where}", params)
        rows = await self._all(
            f"SELECT {_PO_COLUMNS}, p.name AS supplier_name FROM purchase_orders po "
            f"JOIN partners p ON p.id = po.supplier_id{where} "
            "ORDER BY po.order_date DESC, po.po_number DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def get_receipt(self, identifier: str) -> dict | None:
        return await self._one(
            "SELECT id, grn_number, po_id, warehouse_id, receipt_date, value_minor, "
            "entry_id, created_at, created_by FROM goods_receipts "
            "WHERE id = ? OR grn_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def receipt_lines(self, grn_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.line_no, l.po_line_id, l.item_id, l.qty_milli, l.unit_cost_minor, "
            "l.value_minor, i.sku, i.name AS item_name FROM goods_receipt_lines l "
            "JOIN items i ON i.id = l.item_id WHERE l.grn_id = ? ORDER BY l.line_no",
            (grn_id,),
        )

    async def receipts_for_order(self, po_id: str) -> list[dict]:
        return await self._all(
            "SELECT id, grn_number, receipt_date, value_minor, entry_id FROM goods_receipts "
            "WHERE po_id = ? ORDER BY receipt_date, grn_number",
            (po_id,),
        )

    async def get_bill(self, identifier: str) -> dict | None:
        return await self._one(
            "SELECT b.id, b.bill_number, b.supplier_ref, b.supplier_id, b.po_id, b.bill_date, "
            "b.due_date, b.status, b.total_minor, b.amount_paid_minor, b.entry_id, "
            "b.created_at, b.updated_at, b.created_by, p.name AS supplier_name, "
            "po.po_number FROM vendor_bills b "
            "JOIN partners p ON p.id = b.supplier_id "
            "JOIN purchase_orders po ON po.id = b.po_id "
            "WHERE b.id = ? OR b.bill_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def bill_lines(self, bill_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.line_no, l.po_line_id, l.item_id, l.qty_milli, l.unit_price_minor, "
            "l.line_total_minor, i.sku, i.name AS item_name FROM vendor_bill_lines l "
            "JOIN items i ON i.id = l.item_id WHERE l.bill_id = ? ORDER BY l.line_no",
            (bill_id,),
        )

    async def search_bills(
        self, supplier_id: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if supplier_id:
            where += " AND b.supplier_id = ?"
            params.append(supplier_id)
        if status:
            where += " AND b.status = ?"
            params.append(status)
        total = await self._count(f"SELECT COUNT(*) FROM vendor_bills b{where}", params)
        rows = await self._all(
            "SELECT b.id, b.bill_number, b.bill_date, b.due_date, b.status, b.total_minor, "
            "b.amount_paid_minor, p.name AS supplier_name FROM vendor_bills b "
            f"JOIN partners p ON p.id = b.supplier_id{where} "
            "ORDER BY b.bill_date DESC, b.bill_number DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def open_bills(self, as_of: str | None) -> list[dict]:
        sql = (
            "SELECT b.bill_number, b.bill_date, b.due_date, b.total_minor, "
            "b.amount_paid_minor, b.total_minor - b.amount_paid_minor AS outstanding_minor, "
            "p.name AS supplier_name FROM vendor_bills b "
            "JOIN partners p ON p.id = b.supplier_id "
            "WHERE b.status != 'cancelled' AND b.total_minor > b.amount_paid_minor"
        )
        params: list[Any] = []
        if as_of:
            sql += " AND b.bill_date <= ?"
            params.append(as_of)
        return await self._all(sql + " ORDER BY b.due_date", params)

    # -- writes ---------------------------------------------------------------

    async def insert_order(self, conn: Any, order: dict) -> None:
        await conn.execute(
            "INSERT INTO purchase_orders (id, po_number, supplier_id, warehouse_id, order_date, "
            "total_minor, note, created_at, updated_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                order["id"],
                order["po_number"],
                order["supplier_id"],
                order["warehouse_id"],
                order["order_date"],
                order["total_minor"],
                order.get("note"),
                order["created_at"],
                order["updated_at"],
                order["created_by"],
            ),
        )

    async def insert_order_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO purchase_order_lines (id, po_id, line_no, item_id, qty_milli, "
            "unit_price_minor, line_total_minor) VALUES (?,?,?,?,?,?,?)",
            (
                line["id"],
                line["po_id"],
                line["line_no"],
                line["item_id"],
                line["qty_milli"],
                line["unit_price_minor"],
                line["line_total_minor"],
            ),
        )

    async def set_order_status(self, conn: Any, po_id: str, status: str, now: str) -> None:
        await conn.execute(
            "UPDATE purchase_orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, po_id),
        )

    async def add_received(self, conn: Any, line_id: str, qty_milli: int) -> None:
        await conn.execute(
            "UPDATE purchase_order_lines SET qty_received_milli = qty_received_milli + ? "
            "WHERE id = ?",
            (qty_milli, line_id),
        )

    async def add_billed(self, conn: Any, line_id: str, qty_milli: int) -> None:
        await conn.execute(
            "UPDATE purchase_order_lines SET qty_billed_milli = qty_billed_milli + ? WHERE id = ?",
            (qty_milli, line_id),
        )

    async def insert_receipt(self, conn: Any, receipt: dict) -> None:
        await conn.execute(
            "INSERT INTO goods_receipts (id, grn_number, po_id, warehouse_id, receipt_date, "
            "value_minor, entry_id, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                receipt["id"],
                receipt["grn_number"],
                receipt["po_id"],
                receipt["warehouse_id"],
                receipt["receipt_date"],
                receipt["value_minor"],
                receipt.get("entry_id"),
                receipt["created_at"],
                receipt["created_by"],
            ),
        )

    async def insert_receipt_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO goods_receipt_lines (id, grn_id, line_no, po_line_id, item_id, "
            "qty_milli, unit_cost_minor, value_minor) VALUES (?,?,?,?,?,?,?,?)",
            (
                line["id"],
                line["grn_id"],
                line["line_no"],
                line["po_line_id"],
                line["item_id"],
                line["qty_milli"],
                line["unit_cost_minor"],
                line["value_minor"],
            ),
        )

    async def insert_bill(self, conn: Any, bill: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO vendor_bills (id, bill_number, supplier_ref, supplier_id, po_id, "
                "bill_date, due_date, total_minor, entry_id, created_at, updated_at, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bill["id"],
                    bill["bill_number"],
                    bill.get("supplier_ref"),
                    bill["supplier_id"],
                    bill["po_id"],
                    bill["bill_date"],
                    bill["due_date"],
                    bill["total_minor"],
                    bill.get("entry_id"),
                    bill["created_at"],
                    bill["updated_at"],
                    bill["created_by"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(f"Vendor bill '{bill['bill_number']}' already exists") from exc

    async def insert_bill_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO vendor_bill_lines (id, bill_id, line_no, po_line_id, item_id, "
            "qty_milli, unit_price_minor, line_total_minor) VALUES (?,?,?,?,?,?,?,?)",
            (
                line["id"],
                line["bill_id"],
                line["line_no"],
                line["po_line_id"],
                line["item_id"],
                line["qty_milli"],
                line["unit_price_minor"],
                line["line_total_minor"],
            ),
        )

    async def settle_bill(
        self, conn: Any, bill_id: str, amount_minor: int, status: str, now: str
    ) -> None:
        await conn.execute(
            "UPDATE vendor_bills SET amount_paid_minor = amount_paid_minor + ?, "
            "status = ?, updated_at = ? WHERE id = ?",
            (amount_minor, status, now, bill_id),
        )


# ---------------------------------------------------------------------------
# Order-to-cash
# ---------------------------------------------------------------------------

_SO_COLUMNS = (
    "so.id, so.so_number, so.customer_id, so.warehouse_id, so.order_date, so.status, "
    "so.total_minor, so.note, so.created_at, so.updated_at, so.created_by"
)


class SqliteSalesRepository(_Repository):
    async def get_order(self, identifier: str) -> dict | None:
        return await self._one(
            f"SELECT {_SO_COLUMNS}, p.name AS customer_name, w.code AS warehouse_code "
            "FROM sales_orders so "
            "JOIN partners p ON p.id = so.customer_id "
            "JOIN warehouses w ON w.id = so.warehouse_id "
            "WHERE so.id = ? OR so.so_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def order_lines(self, so_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.id, l.line_no, l.item_id, l.qty_milli, l.unit_price_minor, "
            "l.line_total_minor, l.qty_delivered_milli, l.qty_invoiced_milli, "
            "i.sku, i.name AS item_name, i.kind AS item_kind, i.uom "
            "FROM sales_order_lines l JOIN items i ON i.id = l.item_id "
            "WHERE l.so_id = ? ORDER BY l.line_no",
            (so_id,),
        )

    async def get_line(self, line_id: str) -> dict | None:
        return await self._one(
            "SELECT id, so_id, line_no, item_id, qty_milli, unit_price_minor, "
            "line_total_minor, qty_delivered_milli, qty_invoiced_milli "
            "FROM sales_order_lines WHERE id = ?",
            (line_id,),
        )

    async def search_orders(
        self, customer_id: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if customer_id:
            where += " AND so.customer_id = ?"
            params.append(customer_id)
        if status:
            where += " AND so.status = ?"
            params.append(status)
        total = await self._count(f"SELECT COUNT(*) FROM sales_orders so{where}", params)
        rows = await self._all(
            f"SELECT {_SO_COLUMNS}, p.name AS customer_name FROM sales_orders so "
            f"JOIN partners p ON p.id = so.customer_id{where} "
            "ORDER BY so.order_date DESC, so.so_number DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def get_delivery(self, identifier: str) -> dict | None:
        return await self._one(
            "SELECT id, dn_number, so_id, warehouse_id, delivery_date, cost_minor, entry_id, "
            "created_at, created_by FROM deliveries WHERE id = ? OR dn_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def delivery_lines(self, delivery_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.line_no, l.so_line_id, l.item_id, l.qty_milli, l.unit_cost_minor, "
            "l.value_minor, i.sku, i.name AS item_name FROM delivery_lines l "
            "JOIN items i ON i.id = l.item_id WHERE l.delivery_id = ? ORDER BY l.line_no",
            (delivery_id,),
        )

    async def get_invoice(self, identifier: str) -> dict | None:
        return await self._one(
            "SELECT inv.id, inv.invoice_number, inv.customer_id, inv.so_id, inv.invoice_date, "
            "inv.due_date, inv.status, inv.total_minor, inv.amount_paid_minor, inv.entry_id, "
            "inv.created_at, inv.updated_at, inv.created_by, p.name AS customer_name, "
            "so.so_number FROM customer_invoices inv "
            "JOIN partners p ON p.id = inv.customer_id "
            "JOIN sales_orders so ON so.id = inv.so_id "
            "WHERE inv.id = ? OR inv.invoice_number = ? LIMIT 1",
            (identifier, identifier),
        )

    async def invoice_lines(self, invoice_id: str) -> list[dict]:
        return await self._all(
            "SELECT l.line_no, l.so_line_id, l.item_id, l.qty_milli, l.unit_price_minor, "
            "l.line_total_minor, i.sku, i.name AS item_name FROM customer_invoice_lines l "
            "JOIN items i ON i.id = l.item_id WHERE l.invoice_id = ? ORDER BY l.line_no",
            (invoice_id,),
        )

    async def search_invoices(
        self, customer_id: str | None, status: str | None, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if customer_id:
            where += " AND inv.customer_id = ?"
            params.append(customer_id)
        if status:
            where += " AND inv.status = ?"
            params.append(status)
        total = await self._count(f"SELECT COUNT(*) FROM customer_invoices inv{where}", params)
        rows = await self._all(
            "SELECT inv.id, inv.invoice_number, inv.invoice_date, inv.due_date, inv.status, "
            "inv.total_minor, inv.amount_paid_minor, p.name AS customer_name "
            "FROM customer_invoices inv "
            f"JOIN partners p ON p.id = inv.customer_id{where} "
            "ORDER BY inv.invoice_date DESC, inv.invoice_number DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return rows, total

    async def open_invoices(self, as_of: str | None) -> list[dict]:
        sql = (
            "SELECT inv.invoice_number, inv.invoice_date, inv.due_date, inv.total_minor, "
            "inv.amount_paid_minor, inv.total_minor - inv.amount_paid_minor AS outstanding_minor, "
            "p.name AS customer_name FROM customer_invoices inv "
            "JOIN partners p ON p.id = inv.customer_id "
            "WHERE inv.status != 'cancelled' AND inv.total_minor > inv.amount_paid_minor"
        )
        params: list[Any] = []
        if as_of:
            sql += " AND inv.invoice_date <= ?"
            params.append(as_of)
        return await self._all(sql + " ORDER BY inv.due_date", params)

    # -- writes ---------------------------------------------------------------

    async def insert_order(self, conn: Any, order: dict) -> None:
        await conn.execute(
            "INSERT INTO sales_orders (id, so_number, customer_id, warehouse_id, order_date, "
            "total_minor, note, created_at, updated_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                order["id"],
                order["so_number"],
                order["customer_id"],
                order["warehouse_id"],
                order["order_date"],
                order["total_minor"],
                order.get("note"),
                order["created_at"],
                order["updated_at"],
                order["created_by"],
            ),
        )

    async def insert_order_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO sales_order_lines (id, so_id, line_no, item_id, qty_milli, "
            "unit_price_minor, line_total_minor) VALUES (?,?,?,?,?,?,?)",
            (
                line["id"],
                line["so_id"],
                line["line_no"],
                line["item_id"],
                line["qty_milli"],
                line["unit_price_minor"],
                line["line_total_minor"],
            ),
        )

    async def set_order_status(self, conn: Any, so_id: str, status: str, now: str) -> None:
        await conn.execute(
            "UPDATE sales_orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, so_id),
        )

    async def add_delivered(self, conn: Any, line_id: str, qty_milli: int) -> None:
        await conn.execute(
            "UPDATE sales_order_lines SET qty_delivered_milli = qty_delivered_milli + ? "
            "WHERE id = ?",
            (qty_milli, line_id),
        )

    async def add_invoiced(self, conn: Any, line_id: str, qty_milli: int) -> None:
        await conn.execute(
            "UPDATE sales_order_lines SET qty_invoiced_milli = qty_invoiced_milli + ? WHERE id = ?",
            (qty_milli, line_id),
        )

    async def insert_delivery(self, conn: Any, delivery: dict) -> None:
        await conn.execute(
            "INSERT INTO deliveries (id, dn_number, so_id, warehouse_id, delivery_date, "
            "cost_minor, entry_id, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                delivery["id"],
                delivery["dn_number"],
                delivery["so_id"],
                delivery["warehouse_id"],
                delivery["delivery_date"],
                delivery["cost_minor"],
                delivery.get("entry_id"),
                delivery["created_at"],
                delivery["created_by"],
            ),
        )

    async def insert_delivery_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO delivery_lines (id, delivery_id, line_no, so_line_id, item_id, "
            "qty_milli, unit_cost_minor, value_minor) VALUES (?,?,?,?,?,?,?,?)",
            (
                line["id"],
                line["delivery_id"],
                line["line_no"],
                line["so_line_id"],
                line["item_id"],
                line["qty_milli"],
                line["unit_cost_minor"],
                line["value_minor"],
            ),
        )

    async def insert_invoice(self, conn: Any, invoice: dict) -> None:
        try:
            await conn.execute(
                "INSERT INTO customer_invoices (id, invoice_number, customer_id, so_id, "
                "invoice_date, due_date, total_minor, entry_id, created_at, updated_at, "
                "created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    invoice["id"],
                    invoice["invoice_number"],
                    invoice["customer_id"],
                    invoice["so_id"],
                    invoice["invoice_date"],
                    invoice["due_date"],
                    invoice["total_minor"],
                    invoice.get("entry_id"),
                    invoice["created_at"],
                    invoice["updated_at"],
                    invoice["created_by"],
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ConflictError(f"Invoice '{invoice['invoice_number']}' already exists") from exc

    async def insert_invoice_line(self, conn: Any, line: dict) -> None:
        await conn.execute(
            "INSERT INTO customer_invoice_lines (id, invoice_id, line_no, so_line_id, item_id, "
            "qty_milli, unit_price_minor, line_total_minor) VALUES (?,?,?,?,?,?,?,?)",
            (
                line["id"],
                line["invoice_id"],
                line["line_no"],
                line["so_line_id"],
                line["item_id"],
                line["qty_milli"],
                line["unit_price_minor"],
                line["line_total_minor"],
            ),
        )

    async def settle_invoice(
        self, conn: Any, invoice_id: str, amount_minor: int, status: str, now: str
    ) -> None:
        await conn.execute(
            "UPDATE customer_invoices SET amount_paid_minor = amount_paid_minor + ?, "
            "status = ?, updated_at = ? WHERE id = ?",
            (amount_minor, status, now, invoice_id),
        )


class SqlitePaymentRepository(_Repository):
    async def list_for(self, document_type: str, document_id: str) -> list[dict]:
        return await self._all(
            "SELECT payment_number, kind, amount_minor, payment_date, reference, entry_id "
            "FROM payments WHERE document_type = ? AND document_id = ? ORDER BY payment_date",
            (document_type, document_id),
        )

    async def insert(self, conn: Any, payment: dict) -> None:
        await conn.execute(
            "INSERT INTO payments (id, payment_number, kind, partner_id, document_type, "
            "document_id, amount_minor, payment_date, reference, entry_id, created_at, "
            "created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payment["id"],
                payment["payment_number"],
                payment["kind"],
                payment["partner_id"],
                payment["document_type"],
                payment["document_id"],
                payment["amount_minor"],
                payment["payment_date"],
                payment.get("reference"),
                payment.get("entry_id"),
                payment["created_at"],
                payment["created_by"],
            ),
        )
