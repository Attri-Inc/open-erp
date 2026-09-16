"""Order-to-cash: sales order → delivery → customer invoice → receipt.

The mirror of procure-to-pay, seen from the seller's side. Delivery moves stock
out at average cost and books cost of goods sold; invoicing recognises revenue
and the receivable. The two are deliberately separate postings, because
shipping and billing rarely happen on the same day and the books should show
the gap.

Postings, in order:
    delivery  Dr Cost of goods sold     Cr Inventory
    invoice   Dr Accounts receivable    Cr Revenue
    receipt   Dr Bank                   Cr Accounts receivable
"""

from src.domain.constants import CUSTOMER_KINDS
from src.domain.document_lines import require_line_field, validate_line_keys
from src.domain.errors import ConflictError, NotFoundError, ValidationError
from src.infrastructure.database import Database
from src.infrastructure.identity import add_days, new_id, now_iso, parse_date, today_iso
from src.money import extend, format_minor, format_qty, to_milli
from src.repositories.protocols import (
    AuditWriter,
    PaymentWriter,
    SalesReader,
    SalesWriter,
    SequenceWriter,
)
from src.services.inventory import InventoryService
from src.services.masters import ItemService, PartnerService, WarehouseService
from src.services.posting import PostingService

ORDER_LINE_FIELDS = frozenset({"item", "quantity", "unit_price_minor"})


class SalesService:
    def __init__(
        self,
        db: Database,
        reader: SalesReader,
        writer: SalesWriter,
        partners: PartnerService,
        items: ItemService,
        warehouses: WarehouseService,
        inventory: InventoryService,
        posting: PostingService,
        payments: PaymentWriter,
        sequences: SequenceWriter,
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
        self._audit = audit

    # -- helpers ---------------------------------------------------------------

    async def _resolve_order(self, identifier: str) -> dict:
        order = await self._read.get_order(identifier)
        if not order:
            raise NotFoundError(f"Sales order not found: {identifier}")
        return order

    async def _resolve_invoice(self, identifier: str) -> dict:
        invoice = await self._read.get_invoice(identifier)
        if not invoice:
            raise NotFoundError(f"Customer invoice not found: {identifier}")
        return invoice

    @staticmethod
    def _decorate_line(line: dict) -> dict:
        return {
            **line,
            "quantity": format_qty(line["qty_milli"]),
            "unit_price": format_minor(line["unit_price_minor"]),
            "line_total": format_minor(line["line_total_minor"]),
            "quantity_delivered": format_qty(line.get("qty_delivered_milli", 0)),
            "quantity_invoiced": format_qty(line.get("qty_invoiced_milli", 0)),
        }

    @staticmethod
    def _audit_row(actor: str, action: str, object_id: str, details: str) -> dict:
        return {
            "id": new_id("aud"),
            "actor": actor,
            "action": action,
            "object_type": "sales",
            "object_id": object_id,
            "details": details,
            "created_at": now_iso(),
        }

    @staticmethod
    def _select_lines(
        order_lines: list[dict], requested: list[dict] | None, outstanding_of, label: str
    ) -> list[tuple[dict, int]]:
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

    # -- sales orders ----------------------------------------------------------

    async def create_order(
        self,
        customer: str,
        warehouse: str,
        lines: list[dict],
        order_date: str | None = None,
        note: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Take an order. Lines are {item, quantity, unit_price_minor?} — the
        item's list price is used when no price is given."""
        customer_row = await self._partners.resolve(customer)
        if customer_row["kind"] not in CUSTOMER_KINDS:
            raise ValidationError(
                f"'{customer_row['name']}' is a {customer_row['kind']}, not a customer"
            )
        warehouse_row = await self._warehouses.resolve(warehouse)
        if not lines:
            raise ValidationError("A sales order needs at least one line")

        date = order_date or today_iso()
        parse_date(date, "order_date")

        prepared = []
        total = 0
        for index, line in enumerate(lines, start=1):
            validate_line_keys(line, index, ORDER_LINE_FIELDS)
            require_line_field(line, index, "item")
            require_line_field(line, index, "quantity")
            item = await self._items.resolve(str(line["item"]))
            qty_milli = to_milli(line["quantity"])
            if qty_milli <= 0:
                raise ValidationError(f"Line {index}: quantity must be greater than zero")
            unit_price = int(line.get("unit_price_minor", item["sale_price_minor"]))
            if unit_price < 0:
                raise ValidationError(f"Line {index}: unit_price_minor cannot be negative")
            line_total = extend(qty_milli, unit_price)
            total += line_total
            prepared.append(
                {
                    "id": new_id("sol"),
                    "line_no": index,
                    "item_id": item["id"],
                    "sku": item["sku"],
                    "qty_milli": qty_milli,
                    "unit_price_minor": unit_price,
                    "line_total_minor": line_total,
                }
            )

        so_id = new_id("so")
        now = now_iso()
        async with self._db.unit_of_work() as conn:
            so_number = await self._sequences.next_number(conn, "sales_order")
            await self._write.insert_order(
                conn,
                {
                    "id": so_id,
                    "so_number": so_number,
                    "customer_id": customer_row["id"],
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
                await self._write.insert_order_line(conn, {**line, "so_id": so_id})
            await self._audit.append(
                conn,
                self._audit_row(
                    actor,
                    "create_sales_order",
                    so_id,
                    f"{so_number} · {customer_row['name']} · {format_minor(total)}",
                ),
            )
        return await self.get_order(so_id)

    async def confirm_order(self, order: str, actor: str = "mcp") -> dict:
        row = await self._resolve_order(order)
        if row["status"] != "draft":
            raise ConflictError(
                f"{row['so_number']} is {row['status']}; only a draft order can be confirmed"
            )
        async with self._db.unit_of_work() as conn:
            await self._write.set_order_status(conn, row["id"], "confirmed", now_iso())
            await self._audit.append(
                conn,
                self._audit_row(
                    actor, "confirm_sales_order", row["id"], f"Confirmed {row['so_number']}"
                ),
            )
        return await self.get_order(row["id"])

    async def get_order(self, order: str) -> dict:
        row = await self._resolve_order(order)
        lines = await self._read.order_lines(row["id"])
        return {
            **row,
            "total": format_minor(row["total_minor"]),
            "lines": [self._decorate_line(line) for line in lines],
        }

    async def search_orders(
        self,
        customer: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        customer_id = (await self._partners.resolve(customer))["id"] if customer else None
        rows, total = await self._read.search_orders(customer_id, status, limit, offset)
        items = [{**row, "total": format_minor(row["total_minor"])} for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # -- delivery --------------------------------------------------------------

    async def deliver(
        self,
        order: str,
        lines: list[dict] | None = None,
        delivery_date: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Ship goods: stock out at average cost, value into cost of goods sold.

        Service lines carry no stock and are skipped here — they are billed
        without ever being delivered.
        """
        row = await self._resolve_order(order)
        if row["status"] in ("draft", "cancelled"):
            raise ConflictError(
                f"{row['so_number']} is {row['status']}; confirm the order before delivering"
            )
        order_lines = await self._read.order_lines(row["id"])
        selected = self._select_lines(
            order_lines,
            lines,
            lambda line: line["qty_milli"] - line["qty_delivered_milli"],
            "deliver",
        )
        date = delivery_date or today_iso()
        parse_date(date, "delivery_date")

        cogs_account = await self._posting.account_for_role("cogs")
        inventory_account = await self._posting.account_for_role("inventory")

        delivery_id = new_id("dn")
        now = now_iso()
        delivery_lines = []
        total_cost = 0

        async with self._db.unit_of_work() as conn:
            dn_number = await self._sequences.next_number(conn, "delivery")
            line_no = 0
            for line, qty_milli in selected:
                item = await self._items.resolve(line["item_id"])
                if item["kind"] != "stockable":
                    # Services are invoiced directly; mark them delivered so the
                    # order can complete, but move no stock and post no cost.
                    await self._write.add_delivered(conn, line["id"], qty_milli)
                    continue
                move = await self._inventory.record_out(
                    conn,
                    item,
                    row["warehouse_id"],
                    qty_milli,
                    "delivery",
                    date,
                    "delivery",
                    delivery_id,
                    actor,
                )
                line_no += 1
                total_cost += move["value_minor"]
                delivery_lines.append(
                    {
                        "id": new_id("dnl"),
                        "delivery_id": delivery_id,
                        "line_no": line_no,
                        "so_line_id": line["id"],
                        "item_id": item["id"],
                        "qty_milli": qty_milli,
                        "unit_cost_minor": move["unit_cost_minor"],
                        "value_minor": move["value_minor"],
                        "sku": item["sku"],
                    }
                )
                await self._write.add_delivered(conn, line["id"], qty_milli)

            if not delivery_lines:
                raise ConflictError(f"{row['so_number']} has no stockable quantity left to deliver")

            await self._write.insert_delivery(
                conn,
                {
                    "id": delivery_id,
                    "dn_number": dn_number,
                    "so_id": row["id"],
                    "warehouse_id": row["warehouse_id"],
                    "delivery_date": date,
                    "cost_minor": total_cost,
                    "entry_id": None,
                    "created_at": now,
                    "created_by": actor,
                },
            )
            for line in delivery_lines:
                await self._write.insert_delivery_line(conn, line)

            entry = await self._posting.post(
                conn,
                date,
                f"Delivery {dn_number} · {row['so_number']}",
                [
                    {
                        "account_id": cogs_account["id"],
                        "direction": "debit",
                        "amount_minor": total_cost,
                        "memo": dn_number,
                    },
                    {
                        "account_id": inventory_account["id"],
                        "direction": "credit",
                        "amount_minor": total_cost,
                        "memo": dn_number,
                    },
                ],
                "delivery",
                delivery_id,
                reference=row["so_number"],
                actor=actor,
            )
            await conn.execute(
                "UPDATE deliveries SET entry_id = ? WHERE id = ?", (entry["id"], delivery_id)
            )

            refreshed = await self._read.order_lines(row["id"])
            if all(line["qty_delivered_milli"] >= line["qty_milli"] for line in refreshed):
                await self._write.set_order_status(conn, row["id"], "delivered", now_iso())

            await self._audit.append(
                conn,
                self._audit_row(
                    actor,
                    "deliver_goods",
                    delivery_id,
                    f"{dn_number} against {row['so_number']} · cost {format_minor(total_cost)}",
                ),
            )

        return {
            "id": delivery_id,
            "dn_number": dn_number,
            "so_number": row["so_number"],
            "delivery_date": date,
            "lines": [
                {
                    **line,
                    "quantity": format_qty(line["qty_milli"]),
                    "cost": format_minor(line["value_minor"]),
                }
                for line in delivery_lines
            ],
            "cost_of_goods_sold": format_minor(total_cost),
            "journal_entry": entry,
        }

    # -- invoicing -------------------------------------------------------------

    async def invoice(
        self,
        order: str,
        lines: list[dict] | None = None,
        invoice_date: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Bill the customer: recognise revenue and the receivable.

        Defaults to invoicing everything delivered but not yet invoiced, which
        is the rule that stops an order being billed before it ships.
        """
        row = await self._resolve_order(order)
        if row["status"] == "cancelled":
            raise ConflictError(f"{row['so_number']} is cancelled")
        order_lines = await self._read.order_lines(row["id"])
        selected = self._select_lines(
            order_lines,
            lines,
            lambda line: line["qty_delivered_milli"] - line["qty_invoiced_milli"],
            "invoice",
        )

        customer = await self._partners.resolve(row["customer_id"])
        date = invoice_date or today_iso()
        parse_date(date, "invoice_date")
        due = add_days(date, customer["payment_terms_days"])

        ar_account = await self._posting.account_for_role("ar")
        revenue_account = await self._posting.account_for_role("revenue")

        invoice_id = new_id("inv")
        now = now_iso()
        invoice_lines = []
        total = 0

        async with self._db.unit_of_work() as conn:
            invoice_number = await self._sequences.next_number(conn, "customer_invoice")
            for line_no, (line, qty_milli) in enumerate(selected, start=1):
                line_total = extend(qty_milli, line["unit_price_minor"])
                total += line_total
                invoice_lines.append(
                    {
                        "id": new_id("ivl"),
                        "invoice_id": invoice_id,
                        "line_no": line_no,
                        "so_line_id": line["id"],
                        "item_id": line["item_id"],
                        "qty_milli": qty_milli,
                        "unit_price_minor": line["unit_price_minor"],
                        "line_total_minor": line_total,
                    }
                )
                await self._write.add_invoiced(conn, line["id"], qty_milli)

            await self._write.insert_invoice(
                conn,
                {
                    "id": invoice_id,
                    "invoice_number": invoice_number,
                    "customer_id": customer["id"],
                    "so_id": row["id"],
                    "invoice_date": date,
                    "due_date": due,
                    "total_minor": total,
                    "entry_id": None,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": actor,
                },
            )
            for line in invoice_lines:
                await self._write.insert_invoice_line(conn, line)

            entry = await self._posting.post(
                conn,
                date,
                f"Invoice {invoice_number} · {customer['name']}",
                [
                    {
                        "account_id": ar_account["id"],
                        "direction": "debit",
                        "amount_minor": total,
                        "memo": invoice_number,
                    },
                    {
                        "account_id": revenue_account["id"],
                        "direction": "credit",
                        "amount_minor": total,
                        "memo": invoice_number,
                    },
                ],
                "customer_invoice",
                invoice_id,
                reference=row["so_number"],
                actor=actor,
            )
            await conn.execute(
                "UPDATE customer_invoices SET entry_id = ? WHERE id = ?",
                (entry["id"], invoice_id),
            )

            refreshed = await self._read.order_lines(row["id"])
            if all(line["qty_invoiced_milli"] >= line["qty_milli"] for line in refreshed):
                await self._write.set_order_status(conn, row["id"], "invoiced", now_iso())

            await self._audit.append(
                conn,
                self._audit_row(
                    actor,
                    "post_customer_invoice",
                    invoice_id,
                    f"{invoice_number} against {row['so_number']} · {format_minor(total)}",
                ),
            )

        return {
            "id": invoice_id,
            "invoice_number": invoice_number,
            "so_number": row["so_number"],
            "customer": customer["name"],
            "invoice_date": date,
            "due_date": due,
            "total": format_minor(total),
            "journal_entry": entry,
        }

    async def get_invoice(self, invoice: str) -> dict:
        row = await self._resolve_invoice(invoice)
        lines = await self._read.invoice_lines(row["id"])
        outstanding = row["total_minor"] - row["amount_paid_minor"]
        return {
            **row,
            "total": format_minor(row["total_minor"]),
            "amount_paid": format_minor(row["amount_paid_minor"]),
            "outstanding_minor": outstanding,
            "outstanding": format_minor(outstanding),
            "lines": [self._decorate_line(line) for line in lines],
        }

    async def search_invoices(
        self,
        customer: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        customer_id = (await self._partners.resolve(customer))["id"] if customer else None
        rows, total = await self._read.search_invoices(customer_id, status, limit, offset)
        items = [
            {
                **row,
                "total": format_minor(row["total_minor"]),
                "outstanding": format_minor(row["total_minor"] - row["amount_paid_minor"]),
            }
            for row in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # -- receipt ---------------------------------------------------------------

    async def receive_payment(
        self,
        invoice: str,
        amount_minor: int | None = None,
        payment_date: str | None = None,
        reference: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Apply cash against a customer invoice. Defaults to the full outstanding."""
        row = await self._resolve_invoice(invoice)
        if row["status"] == "cancelled":
            raise ConflictError(f"{row['invoice_number']} is cancelled")
        outstanding = row["total_minor"] - row["amount_paid_minor"]
        if outstanding <= 0:
            raise ConflictError(f"{row['invoice_number']} is already paid in full")

        amount = outstanding if amount_minor is None else int(amount_minor)
        if amount <= 0:
            raise ValidationError("Payment amount must be greater than zero")
        if amount > outstanding:
            raise ConflictError(
                f"Receipt {format_minor(amount)} exceeds the outstanding "
                f"{format_minor(outstanding)} on {row['invoice_number']}"
            )

        date = payment_date or today_iso()
        parse_date(date, "payment_date")
        bank_account = await self._posting.account_for_role("bank")
        ar_account = await self._posting.account_for_role("ar")

        payment_id = new_id("pay")
        status = "paid" if amount == outstanding else "posted"

        async with self._db.unit_of_work() as conn:
            payment_number = await self._sequences.next_number(conn, "payment")
            entry = await self._posting.post(
                conn,
                date,
                f"Receipt {payment_number} · {row['invoice_number']}",
                [
                    {
                        "account_id": bank_account["id"],
                        "direction": "debit",
                        "amount_minor": amount,
                        "memo": payment_number,
                    },
                    {
                        "account_id": ar_account["id"],
                        "direction": "credit",
                        "amount_minor": amount,
                        "memo": payment_number,
                    },
                ],
                "customer_payment",
                payment_id,
                reference=reference,
                actor=actor,
            )
            await self._payments.insert(
                conn,
                {
                    "id": payment_id,
                    "payment_number": payment_number,
                    "kind": "receipt",
                    "partner_id": row["customer_id"],
                    "document_type": "customer_invoice",
                    "document_id": row["id"],
                    "amount_minor": amount,
                    "payment_date": date,
                    "reference": reference,
                    "entry_id": entry["id"],
                    "created_at": now_iso(),
                    "created_by": actor,
                },
            )
            await self._write.settle_invoice(conn, row["id"], amount, status, now_iso())
            await self._audit.append(
                conn,
                self._audit_row(
                    actor,
                    "receive_customer_payment",
                    payment_id,
                    f"{payment_number} · {format_minor(amount)} against {row['invoice_number']}",
                ),
            )

        return {
            "payment_number": payment_number,
            "invoice_number": row["invoice_number"],
            "amount": format_minor(amount),
            "outstanding_after": format_minor(outstanding - amount),
            "status": status,
            "journal_entry": entry,
        }
