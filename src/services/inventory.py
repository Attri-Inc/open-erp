"""Inventory use cases — stock movement and AVCO valuation.

Quantity and value are one thing, not two. Every movement writes an immutable
`stock_moves` row *and* shifts the item's valuation pool in the same statement,
so "how many do we have" and "what are they worth" can never drift apart.

Costing is average cost (AVCO): the pool holds a total quantity and a total
value, and the unit cost is always value ÷ quantity. FIFO would slot in behind
the same two methods (`record_in` / `record_out`) without touching a caller.

`record_in` and `record_out` take the caller's Unit of Work connection, because
a receipt's stock move, its valuation shift and its journal entry must commit
together or not at all.
"""

from typing import Any

from src.domain.errors import ConflictError, ValidationError
from src.infrastructure.database import Database
from src.infrastructure.identity import new_id, now_iso, parse_date, today_iso
from src.money import extend, format_minor, format_qty, to_milli, unit_cost
from src.repositories.protocols import (
    AuditWriter,
    ItemReader,
    ItemWriter,
    StockReader,
    StockWriter,
)
from src.services.masters import ItemService, WarehouseService
from src.services.posting import PostingService


class InventoryService:
    def __init__(
        self,
        db: Database,
        items: ItemService,
        warehouses: WarehouseService,
        item_reader: ItemReader,
        item_writer: ItemWriter,
        stock_reader: StockReader,
        stock_writer: StockWriter,
        posting: PostingService,
        audit: AuditWriter,
    ):
        self._db = db
        self._items = items
        self._warehouses = warehouses
        self._item_reader = item_reader
        self._item_writer = item_writer
        self._stock_read = stock_reader
        self._stock_write = stock_writer
        self._posting = posting
        self._audit = audit

    # -- movement primitives (called inside a caller's Unit of Work) -----------

    async def _write_move(
        self,
        conn: Any,
        item: dict,
        warehouse_id: str,
        direction: str,
        qty_milli: int,
        unit_cost_minor: int,
        value_minor: int,
        reason: str,
        source_type: str | None,
        source_id: str | None,
        moved_at: str,
        actor: str,
    ) -> dict:
        move = {
            "id": new_id("mov"),
            "item_id": item["id"],
            "warehouse_id": warehouse_id,
            "direction": direction,
            "qty_milli": qty_milli,
            "unit_cost_minor": unit_cost_minor,
            "value_minor": value_minor,
            "reason": reason,
            "source_type": source_type,
            "source_id": source_id,
            "moved_at": moved_at,
            "created_at": now_iso(),
            "created_by": actor,
        }
        await self._stock_write.insert_move(conn, move)
        signed_qty = qty_milli if direction == "in" else -qty_milli
        signed_value = value_minor if direction == "in" else -value_minor
        await self._item_writer.apply_valuation(
            conn, item["id"], signed_qty, signed_value, now_iso()
        )
        return move

    @staticmethod
    def _assert_stockable(item: dict, action: str) -> None:
        if item["kind"] != "stockable":
            raise ValidationError(
                f"Cannot {action} '{item['sku']}': it is a service, which is never stocked"
            )

    async def record_in(
        self,
        conn: Any,
        item: dict,
        warehouse_id: str,
        qty_milli: int,
        unit_cost_minor: int,
        reason: str,
        moved_at: str,
        source_type: str | None = None,
        source_id: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Add stock at a known cost. The pool absorbs it; the average shifts."""
        self._assert_stockable(item, "receive")
        if qty_milli <= 0:
            raise ValidationError("Receipt quantity must be greater than zero")
        value = extend(qty_milli, unit_cost_minor)
        return await self._write_move(
            conn,
            item,
            warehouse_id,
            "in",
            qty_milli,
            unit_cost_minor,
            value,
            reason,
            source_type,
            source_id,
            moved_at,
            actor,
        )

    async def record_out(
        self,
        conn: Any,
        item: dict,
        warehouse_id: str,
        qty_milli: int,
        reason: str,
        moved_at: str,
        source_type: str | None = None,
        source_id: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Remove stock at the current average cost.

        Refuses to go negative: an ERP that lets stock fall below zero is
        reporting a number nobody can act on. When the move empties the pool it
        releases the pool's exact remaining value, so repeated rounding can
        never strand a cent of inventory value on a zero-quantity item.
        """
        self._assert_stockable(item, "issue")
        if qty_milli <= 0:
            raise ValidationError("Issue quantity must be greater than zero")

        available = await self._stock_read.on_hand(item["id"], warehouse_id)
        if qty_milli > available:
            raise ConflictError(
                f"Insufficient stock for '{item['sku']}': "
                f"{format_qty(available)} on hand, {format_qty(qty_milli)} requested"
            )

        fresh = await self._item_reader.get(item["id"]) or item
        pool_qty = int(fresh["on_hand_milli"])
        pool_value = int(fresh["value_minor"])
        cost = unit_cost(pool_value, pool_qty)
        value = pool_value if qty_milli == pool_qty else extend(qty_milli, cost)
        return await self._write_move(
            conn,
            fresh,
            warehouse_id,
            "out",
            qty_milli,
            cost,
            value,
            reason,
            source_type,
            source_id,
            moved_at,
            actor,
        )

    # -- public use cases ------------------------------------------------------

    async def adjust(
        self,
        item: str,
        warehouse: str,
        quantity: float,
        reason: str = "Stock adjustment",
        adjusted_at: str | None = None,
        unit_cost_minor: int | None = None,
        actor: str = "mcp",
    ) -> dict:
        """Correct on-hand stock to reality, posting the value change to the GL.

        A positive quantity adds stock (debit Inventory, credit the adjustment
        expense); a negative quantity writes it off the other way. Counting
        stock without booking the value change is how inventory and the
        balance sheet drift apart.
        """
        item_row = await self._items.resolve(item)
        self._assert_stockable(item_row, "adjust")
        warehouse_row = await self._warehouses.resolve(warehouse)
        qty_milli = to_milli(quantity)
        if qty_milli == 0:
            raise ValidationError("Adjustment quantity cannot be zero")
        moved_at = adjusted_at or today_iso()
        parse_date(moved_at, "adjusted_at")

        inventory_account = await self._posting.account_for_role("inventory")
        expense_account = await self._posting.account_for_role("expense")

        async with self._db.unit_of_work() as conn:
            if qty_milli > 0:
                cost = unit_cost_minor
                if cost is None:
                    cost = unit_cost(item_row["value_minor"], item_row["on_hand_milli"])
                if cost <= 0:
                    raise ValidationError(
                        f"'{item_row['sku']}' has no cost yet — pass unit_cost_minor "
                        "so the increase can be valued"
                    )
                move = await self.record_in(
                    conn,
                    item_row,
                    warehouse_row["id"],
                    qty_milli,
                    cost,
                    "adjustment",
                    moved_at,
                    "adjustment",
                    None,
                    actor,
                )
                lines = [
                    {
                        "account_id": inventory_account["id"],
                        "direction": "debit",
                        "amount_minor": move["value_minor"],
                        "memo": reason,
                    },
                    {
                        "account_id": expense_account["id"],
                        "direction": "credit",
                        "amount_minor": move["value_minor"],
                        "memo": reason,
                    },
                ]
            else:
                move = await self.record_out(
                    conn,
                    item_row,
                    warehouse_row["id"],
                    -qty_milli,
                    "adjustment",
                    moved_at,
                    "adjustment",
                    None,
                    actor,
                )
                lines = [
                    {
                        "account_id": expense_account["id"],
                        "direction": "debit",
                        "amount_minor": move["value_minor"],
                        "memo": reason,
                    },
                    {
                        "account_id": inventory_account["id"],
                        "direction": "credit",
                        "amount_minor": move["value_minor"],
                        "memo": reason,
                    },
                ]

            entry = await self._posting.post(
                conn,
                moved_at,
                f"Stock adjustment · {item_row['sku']} · {reason}",
                lines,
                "adjustment",
                move["id"],
                actor=actor,
            )
            await self._audit.append(
                conn,
                {
                    "id": new_id("aud"),
                    "actor": actor,
                    "action": "adjust_stock",
                    "object_type": "item",
                    "object_id": item_row["id"],
                    "details": (
                        f"{format_qty(qty_milli)} {item_row['uom']} of {item_row['sku']} "
                        f"at {warehouse_row['code']} · {reason}"
                    ),
                    "created_at": now_iso(),
                },
            )
        return {
            "item": item_row["sku"],
            "warehouse": warehouse_row["code"],
            "quantity": format_qty(qty_milli),
            "value": format_minor(move["value_minor"]),
            "journal_entry": entry,
        }

    # -- reads -----------------------------------------------------------------

    async def stock_on_hand(self, item: str | None = None) -> dict:
        item_id = None
        if item:
            item_id = (await self._items.resolve(item))["id"]
        rows = await self._item_reader.on_hand_by_warehouse(item_id)
        return {
            "lines": [{**row, "quantity": format_qty(row["qty_milli"])} for row in rows],
            "count": len(rows),
        }

    async def valuation(self) -> dict:
        items = await self._item_reader.list_rows("stockable", False, None)
        lines = []
        total = 0
        for row in items:
            if row["on_hand_milli"] == 0 and row["value_minor"] == 0:
                continue
            total += row["value_minor"]
            lines.append(
                {
                    "sku": row["sku"],
                    "name": row["name"],
                    "on_hand": format_qty(row["on_hand_milli"]),
                    "unit_cost": format_minor(unit_cost(row["value_minor"], row["on_hand_milli"])),
                    "value_minor": row["value_minor"],
                    "value": format_minor(row["value_minor"]),
                }
            )
        return {
            "method": "AVCO",
            "lines": lines,
            "total_value_minor": total,
            "total_value": format_minor(total),
        }

    async def list_moves(
        self,
        item: str | None = None,
        warehouse: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        item_id = (await self._items.resolve(item))["id"] if item else None
        warehouse_id = (await self._warehouses.resolve(warehouse))["id"] if warehouse else None
        rows, total = await self._stock_read.moves(
            item_id, warehouse_id, date_from, date_to, limit, offset
        )
        items = [
            {
                **row,
                "quantity": format_qty(row["qty_milli"]),
                "unit_cost": format_minor(row["unit_cost_minor"]),
                "value": format_minor(row["value_minor"]),
            }
            for row in rows
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
