"""Procure-to-pay: purchase order → goods receipt → vendor bill → payment.

The chain's control point is three-way matching. A vendor bill is checked
against the purchase order (price) and the goods receipts (quantity) before it
posts, and a failed match raises rather than warns — an invoice that nobody
verified is how an ERP quietly pays for goods it never received.

Postings, in order:
    receipt   Dr Inventory (or Expense, for services)   Cr GRNI
    bill      Dr GRNI                                   Cr Accounts payable
    payment   Dr Accounts payable                       Cr Bank

GRNI ("goods received, not invoiced") is the interim account that makes the
match auditable: its balance *is* the list of things received but not billed.
"""

from src.domain.constants import SUPPLIER_KINDS
from src.domain.document_lines import require_line_field, validate_line_keys
from src.domain.errors import ConflictError, MatchError, NotFoundError, ValidationError
from src.infrastructure.database import Database
from src.infrastructure.identity import add_days, new_id, now_iso, parse_date, today_iso
from src.money import extend, format_minor, format_qty, to_milli
from src.repositories.protocols import (
    AuditWriter,
    PaymentWriter,
    PurchaseReader,
    PurchaseWriter,
    SequenceWriter,
    SettingsReader,
)
from src.services.inventory import InventoryService
from src.services.masters import ItemService, PartnerService, WarehouseService
from src.services.posting import PostingService

ORDER_LINE_FIELDS = frozenset({"item", "quantity", "unit_price_minor"})


class PurchasingService:
    def __init__(
        self,
        db: Database,
        reader: PurchaseReader,
        writer: PurchaseWriter,
        partners: PartnerService,
        items: ItemService,
        warehouses: WarehouseService,
        inventory: InventoryService,
        posting: PostingService,
        payments: PaymentWriter,
        sequences: SequenceWriter,
        settings: SettingsReader,
        audit: AuditWriter,
    ):
        self._db = db
        self._read = reader
        self._write = writer
        self._partners = partners
        self._items = items
        self._warehouses = warehouses
        self._inventory = inventory
        self._posting = posting
        self._payments = payments
        self._sequences = sequences
        self._settings = settings
        self._audit = audit

    # -- helpers ---------------------------------------------------------------

    async def _resolve_order(self, identifier: str) -> dict:
        order = await self._read.get_order(identifier)
        if not order:
            raise NotFoundError(f"Purchase order not found: {identifier}")
        return order

    async def _resolve_bill(self, identifier: str) -> dict:
        bill = await self._read.get_bill(identifier)
        if not bill:
            raise NotFoundError(f"Vendor bill not found: {identifier}")
        return bill

    @staticmethod
    def _decorate_line(line: dict) -> dict:
        return {
            **line,
            "quantity": format_qty(line["qty_milli"]),
            "unit_price": format_minor(line["unit_price_minor"]),
            "line_total": format_minor(line["line_total_minor"]),
            "quantity_received": format_qty(line.get("qty_received_milli", 0)),
            "quantity_billed": format_qty(line.get("qty_billed_milli", 0)),
        }

    async def _audit_row(self, actor: str, action: str, object_id: str, details: str) -> dict:
        return {
            "id": new_id("aud"),
            "actor": actor,
            "action": action,
            "object_type": "purchase",
            "object_id": object_id,
            "details": details,
            "created_at": now_iso(),
        }

    @staticmethod
    def _select_lines(
        order_lines: list[dict], requested: list[dict] | None, outstanding_of, label: str
    ) -> list[tuple[dict, int]]:
        """Pair each requested quantity with its order line, defaulting to
        everything still outstanding when the caller names no lines."""
        by_line_no = {line["line_no"]: line for line in order_lines}
        selected: list[tuple[dict, int]] = []

        if requested is None:
            for line in order_lines:
                remaining = outstanding_of(line)
                if remaining > 0:
                    selected.append((line, remaining))
            if not selected:
                raise ConflictError(f"Nothing left to {label} on this order")
            return selected

        for entry in requested:
            line_no = int(entry["line_no"])
            if line_no not in by_line_no:
                raise NotFoundError(f"Order has no line {line_no}")
            line = by_line_no[line_no]
            qty_milli = to_milli(entry["quantity"])
            if qty_milli <= 0:
                raise ValidationError(f"Line {line_no}: quantity must be greater than zero")
            remaining = outstanding_of(line)
            if qty_milli > remaining:
                raise ConflictError(
                    f"Line {line_no}: only {format_qty(remaining)} left to {label}, "
                    f"{format_qty(qty_milli)} requested"
                )
            selected.append((line, qty_milli))
        return selected

    # -- purchase orders -------------------------------------------------------

    async def create_order(
        self,
        supplier: str,
        warehouse: str,
        lines: list[dict],
        order_date: str | None = None,
        note: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Raise a purchase order. Lines are {item, quantity, unit_price_minor}."""
        supplier_row = await self._partners.resolve(supplier)
        if supplier_row["kind"] not in SUPPLIER_KINDS:
            raise ValidationError(
                f"'{supplier_row['name']}' is a {supplier_row['kind']}, not a supplier"
            )
        warehouse_row = await self._warehouses.resolve(warehouse)
        if not lines:
            raise ValidationError("A purchase order needs at least one line")

        date = order_date or today_iso()
        parse_date(date, "order_date")

        prepared = []
        total = 0
        for index, line in enumerate(lines, start=1):
            validate_line_keys(line, index, ORDER_LINE_FIELDS)
            require_line_field(line, index, "item")
            require_line_field(line, index, "quantity")
            require_line_field(line, index, "unit_price_minor")
            item = await self._items.resolve(str(line["item"]))
            qty_milli = to_milli(line["quantity"])
            if qty_milli <= 0:
                raise ValidationError(f"Line {index}: quantity must be greater than zero")
            unit_price = int(line["unit_price_minor"])
            if unit_price < 0:
                raise ValidationError(f"Line {index}: unit_price_minor cannot be negative")
            line_total = extend(qty_milli, unit_price)
            total += line_total
            prepared.append(
                {
                    "id": new_id("pol"),
                    "line_no": index,
                    "item_id": item["id"],
                    "sku": item["sku"],
                    "qty_milli": qty_milli,
                    "unit_price_minor": unit_price,
                    "line_total_minor": line_total,
                }
            )

        po_id = new_id("po")
        now = now_iso()
        async with self._db.unit_of_work() as conn:
            po_number = await self._sequences.next_number(conn, "purchase_order")
            await self._write.insert_order(
                conn,
                {
                    "id": po_id,
                    "po_number": po_number,
                    "supplier_id": supplier_row["id"],
                    "warehouse_id": warehouse_row["id"],
                    "order_date": date,
                    "total_minor": total,
                    "note": note,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor,
                },
            )
            for line in prepared:
                await self._write.insert_order_line(conn, {**line, "po_id": po_id})
            await self._audit.append(
                conn,
                await self._audit_row(
                    actor,
                    "create_purchase_order",
                    po_id,
                    f"{po_number} · {supplier_row['name']} · {format_minor(total)}",
                ),
            )
        return await self.get_order(po_id)

    async def confirm_order(self, order: str, actor: str = "mcp") -> dict:
        """Commit a draft order to the supplier. Only confirmed orders receive."""
        row = await self._resolve_order(order)
        if row["status"] != "draft":
            raise ConflictError(
                f"{row['po_number']} is {row['status']}; only a draft order can be confirmed"
            )
        async with self._db.unit_of_work() as conn:
            await self._write.set_order_status(conn, row["id"], "confirmed", now_iso())
            await self._audit.append(
                conn,
                await self._audit_row(
                    actor, "confirm_purchase_order", row["id"], f"Confirmed {row['po_number']}"
                ),
            )
        return await self.get_order(row["id"])

    async def get_order(self, order: str) -> dict:
        row = await self._resolve_order(order)
        lines = await self._read.order_lines(row["id"])
        receipts = await self._read.receipts_for_order(row["id"])
        return {
            **row,
            "total": format_minor(row["total_minor"]),
            "lines": [self._decorate_line(line) for line in lines],
            "receipts": [{**r, "value": format_minor(r["value_minor"])} for r in receipts],
        }

    async def search_orders(
        self,
        supplier: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        supplier_id = (await self._partners.resolve(supplier))["id"] if supplier else None
        rows, total = await self._read.search_orders(supplier_id, status, limit, offset)
        items = [{**row, "total": format_minor(row["total_minor"])} for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # -- goods receipt ---------------------------------------------------------

    async def receive(
        self,
        order: str,
        lines: list[dict] | None = None,
        receipt_date: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Book a delivery from the supplier: stock in, value into GRNI.

        With no `lines`, everything still outstanding on the order is received.
        """
        row = await self._resolve_order(order)
        if row["status"] in ("draft", "cancelled"):
            raise ConflictError(
                f"{row['po_number']} is {row['status']}; confirm the order before receiving"
            )
        order_lines = await self._read.order_lines(row["id"])
        selected = self._select_lines(
            order_lines,
            lines,
            lambda line: line["qty_milli"] - line["qty_received_milli"],
            "receive",
        )
        date = receipt_date or today_iso()
        parse_date(date, "receipt_date")

        inventory_account = await self._posting.account_for_role("inventory")
        expense_account = await self._posting.account_for_role("expense")
        grni_account = await self._posting.account_for_role("grni")

        grn_id = new_id("grn")
        now = now_iso()
        receipt_lines: list[dict] = []
        debits: dict[str, int] = {}
        total_value = 0

        async with self._db.unit_of_work() as conn:
            grn_number = await self._sequences.next_number(conn, "goods_receipt")
            for line_no, (line, qty_milli) in enumerate(selected, start=1):
                item = await self._items.resolve(line["item_id"])
                unit_cost = line["unit_price_minor"]
                if item["kind"] == "stockable":
                    move = await self._inventory.record_in(
                        conn,
                        item,
                        row["warehouse_id"],
                        qty_milli,
                        unit_cost,
                        "receipt",
                        date,
                        "goods_receipt",
                        grn_id,
                        actor,
                    )
                    value = move["value_minor"]
                    target = inventory_account["id"]
                else:
                    # A service is consumed the moment it is performed; it has no
                    # stock move and lands directly in expense.
                    value = extend(qty_milli, unit_cost)
                    target = expense_account["id"]

                debits[target] = debits.get(target, 0) + value
                total_value += value
                receipt_lines.append(
                    {
                        "id": new_id("grl"),
                        "grn_id": grn_id,
                        "line_no": line_no,
                        "po_line_id": line["id"],
                        "item_id": item["id"],
                        "qty_milli": qty_milli,
                        "unit_cost_minor": unit_cost,
                        "value_minor": value,
                        "sku": item["sku"],
                    }
                )
                await self._write.add_received(conn, line["id"], qty_milli)

            await self._write.insert_receipt(
                conn,
                {
                    "id": grn_id,
                    "grn_number": grn_number,
                    "po_id": row["id"],
                    "warehouse_id": row["warehouse_id"],
                    "receipt_date": date,
                    "value_minor": total_value,
                    "entry_id": None,
                    "created_at": now,
                    "created_by": actor,
                },
            )
            for line in receipt_lines:
                await self._write.insert_receipt_line(conn, line)

            entry = await self._posting.post(
                conn,
                date,
                f"Goods receipt {grn_number} · {row['po_number']}",
                [
                    {
                        "account_id": account_id,
                        "direction": "debit",
                        "amount_minor": amount,
                        "memo": grn_number,
                    }
                    for account_id, amount in debits.items()
                ]
                + [
                    {
                        "account_id": grni_account["id"],
                        "direction": "credit",
                        "amount_minor": total_value,
                        "memo": grn_number,
                    }
                ],
                "goods_receipt",
                grn_id,
                reference=row["po_number"],
                actor=actor,
            )
            await conn.execute(
                "UPDATE goods_receipts SET entry_id = ? WHERE id = ?", (entry["id"], grn_id)
            )

            refreshed = await self._read.order_lines(row["id"])
            if all(line["qty_received_milli"] >= line["qty_milli"] for line in refreshed):
                await self._write.set_order_status(conn, row["id"], "received", now_iso())

            await self._audit.append(
                conn,
                await self._audit_row(
                    actor,
                    "receive_goods",
                    grn_id,
                    f"{grn_number} against {row['po_number']} · {format_minor(total_value)}",
                ),
            )

        return {
            "id": grn_id,
            "grn_number": grn_number,
            "po_number": row["po_number"],
            "receipt_date": date,
            "lines": [
                {
                    **line,
                    "quantity": format_qty(line["qty_milli"]),
                    "value": format_minor(line["value_minor"]),
                }
                for line in receipt_lines
            ],
            "value": format_minor(total_value),
            "journal_entry": entry,
        }

    # -- three-way match -------------------------------------------------------

    async def _match(
        self, order: dict, order_lines: list[dict], proposed: list[dict]
    ) -> list[dict]:
        """Compare proposed bill lines against the PO (price) and receipts
        (quantity). Returns every discrepancy found, not just the first."""
        settings = await self._settings.get() or {}
        tolerance = int(settings.get("price_tolerance_minor", 0))
        by_line_no = {line["line_no"]: line for line in order_lines}
        discrepancies: list[dict] = []

        for entry in proposed:
            line_no = int(entry["line_no"])
            line = by_line_no.get(line_no)
            if not line:
                discrepancies.append(
                    {
                        "line_no": line_no,
                        "check": "line_exists",
                        "detail": f"{order['po_number']} has no line {line_no}",
                    }
                )
                continue

            qty_milli = to_milli(entry["quantity"])
            unit_price = int(entry.get("unit_price_minor", line["unit_price_minor"]))

            if abs(unit_price - line["unit_price_minor"]) > tolerance:
                discrepancies.append(
                    {
                        "line_no": line_no,
                        "check": "price",
                        "detail": (
                            f"billed at {format_minor(unit_price)}, "
                            f"ordered at {format_minor(line['unit_price_minor'])}"
                        ),
                    }
                )

            billable = line["qty_received_milli"] - line["qty_billed_milli"]
            if qty_milli > billable:
                discrepancies.append(
                    {
                        "line_no": line_no,
                        "check": "quantity",
                        "detail": (
                            f"billed {format_qty(qty_milli)}, but only "
                            f"{format_qty(billable)} is received and not yet billed"
                        ),
                    }
                )
        return discrepancies

    async def match_bill(self, order: str, lines: list[dict] | None = None) -> dict:
        """Dry-run the three-way match without posting anything."""
        row = await self._resolve_order(order)
        order_lines = await self._read.order_lines(row["id"])
        proposed = (
            lines
            if lines is not None
            else [
                {
                    "line_no": line["line_no"],
                    "quantity": (line["qty_received_milli"] - line["qty_billed_milli"]) / 1000,
                    "unit_price_minor": line["unit_price_minor"],
                }
                for line in order_lines
                if line["qty_received_milli"] > line["qty_billed_milli"]
            ]
        )
        if not proposed:
            return {
                "po_number": row["po_number"],
                "matched": False,
                "discrepancies": [
                    {"check": "quantity", "detail": "Nothing received is waiting to be billed"}
                ],
            }
        discrepancies = await self._match(row, order_lines, proposed)
        return {
            "po_number": row["po_number"],
            "matched": not discrepancies,
            "checked_lines": len(proposed),
            "discrepancies": discrepancies,
        }

    async def bill(
        self,
        order: str,
        lines: list[dict] | None = None,
        bill_date: str | None = None,
        supplier_ref: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Post a supplier's invoice, but only if it matches the PO and receipts."""
        row = await self._resolve_order(order)
        if row["status"] == "cancelled":
            raise ConflictError(f"{row['po_number']} is cancelled")
        order_lines = await self._read.order_lines(row["id"])

        proposed = (
            lines
            if lines is not None
            else [
                {
                    "line_no": line["line_no"],
                    "quantity": (line["qty_received_milli"] - line["qty_billed_milli"]) / 1000,
                    "unit_price_minor": line["unit_price_minor"],
                }
                for line in order_lines
                if line["qty_received_milli"] > line["qty_billed_milli"]
            ]
        )
        if not proposed:
            raise ConflictError(
                f"Nothing to bill on {row['po_number']}: no received quantity is unbilled"
            )

        discrepancies = await self._match(row, order_lines, proposed)
        if discrepancies:
            raise MatchError(
                f"Three-way match failed for {row['po_number']}: "
                f"{len(discrepancies)} discrepancy(ies)",
                discrepancies,
            )

        supplier = await self._partners.resolve(row["supplier_id"])
        date = bill_date or today_iso()
        parse_date(date, "bill_date")
        due = add_days(date, supplier["payment_terms_days"])

        grni_account = await self._posting.account_for_role("grni")
        ap_account = await self._posting.account_for_role("ap")

        by_line_no = {line["line_no"]: line for line in order_lines}
        bill_id = new_id("bil")
        now = now_iso()
        bill_lines = []
        total = 0

        async with self._db.unit_of_work() as conn:
            bill_number = await self._sequences.next_number(conn, "vendor_bill")
            for line_no, entry in enumerate(proposed, start=1):
                po_line = by_line_no[int(entry["line_no"])]
                qty_milli = to_milli(entry["quantity"])
                unit_price = int(entry.get("unit_price_minor", po_line["unit_price_minor"]))
                line_total = extend(qty_milli, unit_price)
                total += line_total
                bill_lines.append(
                    {
                        "id": new_id("bll"),
                        "bill_id": bill_id,
                        "line_no": line_no,
                        "po_line_id": po_line["id"],
                        "item_id": po_line["item_id"],
                        "qty_milli": qty_milli,
                        "unit_price_minor": unit_price,
                        "line_total_minor": line_total,
                    }
                )
                await self._write.add_billed(conn, po_line["id"], qty_milli)

            await self._write.insert_bill(
                conn,
                {
                    "id": bill_id,
                    "bill_number": bill_number,
                    "supplier_ref": supplier_ref,
                    "supplier_id": supplier["id"],
                    "po_id": row["id"],
                    "bill_date": date,
                    "due_date": due,
                    "total_minor": total,
                    "entry_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor,
                },
            )
            for line in bill_lines:
                await self._write.insert_bill_line(conn, line)

            journal = await self._posting.post(
                conn,
                date,
                f"Vendor bill {bill_number} · {supplier['name']}",
                [
                    {
                        "account_id": grni_account["id"],
                        "direction": "debit",
                        "amount_minor": total,
                        "memo": bill_number,
                    },
                    {
                        "account_id": ap_account["id"],
                        "direction": "credit",
                        "amount_minor": total,
                        "memo": bill_number,
                    },
                ],
                "vendor_bill",
                bill_id,
                reference=supplier_ref or row["po_number"],
                actor=actor,
            )
            await conn.execute(
                "UPDATE vendor_bills SET entry_id = ? WHERE id = ?", (journal["id"], bill_id)
            )

            refreshed = await self._read.order_lines(row["id"])
            if all(line["qty_billed_milli"] >= line["qty_milli"] for line in refreshed):
                await self._write.set_order_status(conn, row["id"], "billed", now_iso())

            await self._audit.append(
                conn,
                await self._audit_row(
                    actor,
                    "post_vendor_bill",
                    bill_id,
                    f"{bill_number} against {row['po_number']} · {format_minor(total)} "
                    "· three-way match passed",
                ),
            )

        return {
            "id": bill_id,
            "bill_number": bill_number,
            "po_number": row["po_number"],
            "supplier": supplier["name"],
            "bill_date": date,
            "due_date": due,
            "total": format_minor(total),
            "matched": True,
            "journal_entry": journal,
        }

    async def get_bill(self, bill: str) -> dict:
        row = await self._resolve_bill(bill)
        lines = await self._read.bill_lines(row["id"])
        outstanding = row["total_minor"] - row["amount_paid_minor"]
        return {
            **row,
            "total": format_minor(row["total_minor"]),
            "amount_paid": format_minor(row["amount_paid_minor"]),
            "outstanding_minor": outstanding,
            "outstanding": format_minor(outstanding),
            "lines": [self._decorate_line(line) for line in lines],
        }

    async def search_bills(
        self,
        supplier: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        supplier_id = (await self._partners.resolve(supplier))["id"] if supplier else None
        rows, total = await self._read.search_bills(supplier_id, status, limit, offset)
        items = [
            {
                **row,
                "total": format_minor(row["total_minor"]),
                "outstanding": format_minor(row["total_minor"] - row["amount_paid_minor"]),
            }
            for row in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # -- payment ---------------------------------------------------------------

    async def pay_bill(
        self,
        bill: str,
        amount_minor: int | None = None,
        payment_date: str | None = None,
        reference: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Settle a vendor bill from the bank. Defaults to the full outstanding."""
        row = await self._resolve_bill(bill)
        if row["status"] == "cancelled":
            raise ConflictError(f"{row['bill_number']} is cancelled")
        outstanding = row["total_minor"] - row["amount_paid_minor"]
        if outstanding <= 0:
            raise ConflictError(f"{row['bill_number']} is already paid in full")

        amount = outstanding if amount_minor is None else int(amount_minor)
        if amount <= 0:
            raise ValidationError("Payment amount must be greater than zero")
        if amount > outstanding:
            raise ConflictError(
                f"Payment {format_minor(amount)} exceeds the outstanding "
                f"{format_minor(outstanding)} on {row['bill_number']}"
            )

        date = payment_date or today_iso()
        parse_date(date, "payment_date")
        ap_account = await self._posting.account_for_role("ap")
        bank_account = await self._posting.account_for_role("bank")

        payment_id = new_id("pay")
        status = "paid" if amount == outstanding else "posted"

        async with self._db.unit_of_work() as conn:
            payment_number = await self._sequences.next_number(conn, "payment")
            entry = await self._posting.post(
                conn,
                date,
                f"Payment {payment_number} · {row['bill_number']}",
                [
                    {
                        "account_id": ap_account["id"],
                        "direction": "debit",
                        "amount_minor": amount,
                        "memo": payment_number,
                    },
                    {
                        "account_id": bank_account["id"],
                        "direction": "credit",
                        "amount_minor": amount,
                        "memo": payment_number,
                    },
                ],
                "supplier_payment",
                payment_id,
                reference=reference,
                actor=actor,
            )
            await self._payments.insert(
                conn,
                {
                    "id": payment_id,
                    "payment_number": payment_number,
                    "kind": "disbursement",
                    "partner_id": row["supplier_id"],
                    "document_type": "vendor_bill",
                    "document_id": row["id"],
                    "amount_minor": amount,
                    "payment_date": date,
                    "reference": reference,
                    "entry_id": entry["id"],
                    "created_at": now_iso(),
                    "created_by": actor,
                },
            )
            await self._write.settle_bill(conn, row["id"], amount, status, now_iso())
            await self._audit.append(
                conn,
                await self._audit_row(
                    actor,
                    "pay_vendor_bill",
                    payment_id,
                    f"{payment_number} · {format_minor(amount)} against {row['bill_number']}",
                ),
            )

        return {
            "payment_number": payment_number,
            "bill_number": row["bill_number"],
            "amount": format_minor(amount),
            "outstanding_after": format_minor(outstanding - amount),
            "status": status,
            "journal_entry": entry,
        }
