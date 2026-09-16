"""Master-data use cases: partners, items, warehouses, and the chart of accounts.

Masters are the vocabulary every document speaks in. Each service resolves by
id, code/sku, or name so an agent can say "Acme" and never handle an id.
"""

from src.domain.constants import (
    ACCOUNT_ROLES,
    ITEM_KINDS,
    NORMAL_SIDE,
    PARTNER_KINDS,
)
from src.domain.errors import NotFoundError, ValidationError
from src.infrastructure.database import Database
from src.infrastructure.identity import new_id, now_iso
from src.money import format_minor, format_qty, unit_cost
from src.repositories.protocols import (
    AccountReader,
    AccountWriter,
    AuditWriter,
    ItemReader,
    ItemWriter,
    PartnerReader,
    PartnerWriter,
    WarehouseReader,
    WarehouseWriter,
)


def _audit(actor: str, action: str, object_type: str, object_id: str, details: str) -> dict:
    return {
        "id": new_id("aud"),
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "details": details,
        "created_at": now_iso(),
    }


class PartnerService:
    def __init__(
        self, db: Database, reader: PartnerReader, writer: PartnerWriter, audit: AuditWriter
    ):
        self._db = db
        self._reader = reader
        self._writer = writer
        self._audit = audit

    async def resolve(self, identifier: str) -> dict:
        partner = await self._reader.get(identifier)
        if not partner:
            raise NotFoundError(f"Partner not found: {identifier}")
        return partner

    async def list_partners(
        self, kind: str | None = None, include_archived: bool = False, q: str | None = None
    ) -> list[dict]:
        if kind and kind not in PARTNER_KINDS:
            raise ValidationError(f"Invalid kind '{kind}'. Use: {', '.join(sorted(PARTNER_KINDS))}")
        return await self._reader.list_rows(kind, include_archived, q)

    async def get(self, identifier: str) -> dict:
        return await self.resolve(identifier)

    async def create(
        self,
        code: str,
        name: str,
        kind: str,
        email: str | None = None,
        phone: str | None = None,
        payment_terms_days: int = 30,
        actor: str = "mcp",
    ) -> dict:
        if kind not in PARTNER_KINDS:
            raise ValidationError(f"Invalid kind '{kind}'. Use: {', '.join(sorted(PARTNER_KINDS))}")
        if payment_terms_days < 0:
            raise ValidationError("payment_terms_days cannot be negative")
        now = now_iso()
        partner_id = new_id("prt")
        partner = {
            "id": partner_id,
            "code": code,
            "name": name,
            "kind": kind,
            "email": email,
            "phone": phone,
            "payment_terms_days": payment_terms_days,
            "created_at": now,
            "updated_at": now,
        }
        async with self._db.unit_of_work() as conn:
            await self._writer.insert(conn, partner)
            await self._audit.append(
                conn,
                _audit(
                    actor,
                    "create_partner",
                    "partner",
                    partner_id,
                    f"Created {kind} {code} · {name}",
                ),
            )
        return partner


class ItemService:
    def __init__(self, db: Database, reader: ItemReader, writer: ItemWriter, audit: AuditWriter):
        self._db = db
        self._reader = reader
        self._writer = writer
        self._audit = audit

    async def resolve(self, identifier: str) -> dict:
        item = await self._reader.get(identifier)
        if not item:
            raise NotFoundError(f"Item not found: {identifier}")
        return item

    @staticmethod
    def decorate(item: dict) -> dict:
        """Attach the derived AVCO unit cost and human-readable amounts.

        Unit cost is never stored — it is value ÷ quantity, computed the same
        way everywhere, so it can never disagree with the valuation pool.
        """
        cost = unit_cost(item["value_minor"], item["on_hand_milli"])
        return {
            **item,
            "on_hand": format_qty(item["on_hand_milli"]),
            "unit_cost_minor": cost,
            "unit_cost": format_minor(cost),
            "stock_value": format_minor(item["value_minor"]),
            "sale_price": format_minor(item["sale_price_minor"]),
        }

    async def list_items(
        self, kind: str | None = None, include_archived: bool = False, q: str | None = None
    ) -> list[dict]:
        if kind and kind not in ITEM_KINDS:
            raise ValidationError(f"Invalid kind '{kind}'. Use: {', '.join(sorted(ITEM_KINDS))}")
        return [
            self.decorate(row) for row in await self._reader.list_rows(kind, include_archived, q)
        ]

    async def get(self, identifier: str) -> dict:
        item = self.decorate(await self.resolve(identifier))
        item["by_warehouse"] = [
            {**row, "quantity": format_qty(row["qty_milli"])}
            for row in await self._reader.on_hand_by_warehouse(item["id"])
        ]
        return item

    async def create(
        self,
        sku: str,
        name: str,
        kind: str = "stockable",
        uom: str = "unit",
        sale_price_minor: int = 0,
        actor: str = "mcp",
    ) -> dict:
        if kind not in ITEM_KINDS:
            raise ValidationError(f"Invalid kind '{kind}'. Use: {', '.join(sorted(ITEM_KINDS))}")
        if sale_price_minor < 0:
            raise ValidationError("sale_price_minor cannot be negative")
        now = now_iso()
        item_id = new_id("itm")
        item = {
            "id": item_id,
            "sku": sku,
            "name": name,
            "kind": kind,
            "uom": uom,
            "sale_price_minor": sale_price_minor,
            "on_hand_milli": 0,
            "value_minor": 0,
            "is_archived": 0,
            "created_at": now,
            "updated_at": now,
        }
        async with self._db.unit_of_work() as conn:
            await self._writer.insert(conn, item)
            await self._audit.append(
                conn,
                _audit(actor, "create_item", "item", item_id, f"Created {kind} {sku} · {name}"),
            )
        return self.decorate(item)


class WarehouseService:
    def __init__(
        self, db: Database, reader: WarehouseReader, writer: WarehouseWriter, audit: AuditWriter
    ):
        self._db = db
        self._reader = reader
        self._writer = writer
        self._audit = audit

    async def resolve(self, identifier: str) -> dict:
        warehouse = await self._reader.get(identifier)
        if not warehouse:
            raise NotFoundError(f"Warehouse not found: {identifier}")
        return warehouse

    async def list_warehouses(self, include_archived: bool = False) -> list[dict]:
        return await self._reader.list_rows(include_archived)

    async def create(self, code: str, name: str, actor: str = "mcp") -> dict:
        now = now_iso()
        warehouse = {
            "id": new_id("whs"),
            "code": code,
            "name": name,
            "created_at": now,
            "updated_at": now,
        }
        async with self._db.unit_of_work() as conn:
            await self._writer.insert(conn, warehouse)
            await self._audit.append(
                conn,
                _audit(
                    actor,
                    "create_warehouse",
                    "warehouse",
                    warehouse["id"],
                    f"Created warehouse {code} · {name}",
                ),
            )
        return warehouse


class AccountService:
    def __init__(
        self, db: Database, reader: AccountReader, writer: AccountWriter, audit: AuditWriter
    ):
        self._db = db
        self._reader = reader
        self._writer = writer
        self._audit = audit

    async def resolve(self, identifier: str) -> dict:
        account = await self._reader.get(identifier)
        if not account:
            raise NotFoundError(f"Account not found: {identifier}")
        return account

    async def _signed_balance(self, account: dict, as_of: str | None = None) -> int:
        net = await self._reader.net_debit(account["id"], as_of)
        return net if account["normal_side"] == "debit" else -net

    async def list_accounts(
        self, account_type: str | None = None, include_archived: bool = False
    ) -> list[dict]:
        if account_type and account_type not in NORMAL_SIDE:
            raise ValidationError(
                f"Invalid account type '{account_type}'. Use: {', '.join(sorted(NORMAL_SIDE))}"
            )
        rows = await self._reader.list_rows(account_type, include_archived)
        result = []
        for account in rows:
            balance = await self._signed_balance(account)
            result.append({**account, "balance_minor": balance, "balance": format_minor(balance)})
        return result

    async def balance(self, identifier: str, as_of: str | None = None) -> dict:
        account = await self.resolve(identifier)
        balance = await self._signed_balance(account, as_of)
        return {
            "account_id": account["id"],
            "code": account["code"],
            "name": account["name"],
            "type": account["type"],
            "normal_side": account["normal_side"],
            "as_of": as_of or "latest",
            "balance_minor": balance,
            "balance": format_minor(balance),
        }

    async def ledger(
        self,
        identifier: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
    ) -> dict:
        account = await self.resolve(identifier)
        # Full history up to date_to so the running balance carries the opening
        # balance forward; date_from only filters which rows are displayed.
        rows = await self._reader.ledger_rows(account["id"], date_to)
        sign = 1 if account["normal_side"] == "debit" else -1
        running = 0
        entries = []
        for row in rows:
            running += sign * (
                row["amount_minor"] if row["direction"] == "debit" else -row["amount_minor"]
            )
            row["amount"] = format_minor(row["amount_minor"])
            row["running_balance_minor"] = running
            row["running_balance"] = format_minor(running)
            if date_from is None or row["entry_date"] >= date_from:
                entries.append(row)
        return {
            "account": {
                "id": account["id"],
                "code": account["code"],
                "name": account["name"],
                "type": account["type"],
            },
            "entries": entries[-limit:],
            "ending_balance_minor": running,
            "ending_balance": format_minor(running),
        }

    async def create(
        self,
        code: str,
        name: str,
        account_type: str,
        role: str | None = None,
        description: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        if account_type not in NORMAL_SIDE:
            raise ValidationError(
                f"Invalid account type '{account_type}'. Use: {', '.join(sorted(NORMAL_SIDE))}"
            )
        if role and role not in ACCOUNT_ROLES:
            raise ValidationError(f"Invalid role '{role}'. Use: {', '.join(sorted(ACCOUNT_ROLES))}")
        now = now_iso()
        account_id = new_id("acc")
        account = {
            "id": account_id,
            "code": code,
            "name": name,
            "type": account_type,
            "normal_side": NORMAL_SIDE[account_type],
            "role": role,
            "description": description,
            "created_at": now,
            "updated_at": now,
        }
        async with self._db.unit_of_work() as conn:
            await self._writer.insert(conn, account)
            await self._audit.append(
                conn,
                _audit(
                    actor,
                    "create_account",
                    "account",
                    account_id,
                    f"Created account {code} · {name} ({account_type})",
                ),
            )
        return await self.balance(account_id)
