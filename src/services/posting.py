"""The posting kernel — the single door into the general ledger.

Every document that moves money (goods receipt, vendor bill, delivery,
customer invoice, payment) posts through `PostingService.post`, which is the
one place the double-entry invariants live:

  1. A direction column, never a signed amount — a line is a debit or a credit.
  2. Debits equal credits, and an entry has at least two lines.
  3. Posted entries are immutable; corrections are reversing entries.

`post` takes the caller's Unit of Work connection so the journal entry commits
in the same transaction as the document and stock moves that caused it. If the
posting fails, the document never existed.
"""

from typing import Any

from src.domain.constants import DIRECTIONS
from src.domain.errors import ConflictError, NotFoundError, ValidationError
from src.infrastructure.database import Database
from src.infrastructure.identity import new_id, now_iso, parse_date
from src.money import format_minor
from src.repositories.protocols import (
    AccountReader,
    AuditWriter,
    JournalReader,
    JournalWriter,
    SequenceWriter,
)


class PostingService:
    def __init__(
        self,
        db: Database,
        accounts: AccountReader,
        journal_read: JournalReader,
        journal_write: JournalWriter,
        sequences: SequenceWriter,
        audit: AuditWriter,
    ):
        self._db = db
        self._accounts = accounts
        self._read = journal_read
        self._write = journal_write
        self._sequences = sequences
        self._audit = audit

    # -- account resolution ----------------------------------------------------

    async def account_for_role(self, role: str) -> dict:
        """Posting rules name accounts by meaning ('inventory'), not by code, so
        a deployment can renumber its chart of accounts without code changes."""
        account = await self._accounts.get_by_role(role)
        if not account:
            raise ValidationError(
                f"No account is mapped to the '{role}' role. "
                "Map one with create_account(role=...) before posting."
            )
        return account

    # -- the write path --------------------------------------------------------

    async def post(
        self,
        conn: Any,
        entry_date: str,
        description: str,
        lines: list[dict],
        source_type: str,
        source_id: str | None = None,
        reference: str | None = None,
        actor: str = "mcp",
        reverses_id: str | None = None,
    ) -> dict:
        """Write one balanced journal entry inside the caller's transaction.

        `lines` are dicts of {account_id, direction, amount_minor, memo?}.
        Zero-amount lines are dropped before validation so callers can build
        line lists unconditionally.
        """
        parse_date(entry_date, "entry_date")

        lines = [line for line in lines if int(line["amount_minor"]) != 0]
        if len(lines) < 2:
            raise ValidationError("A journal entry needs at least 2 lines")

        debits = credits = 0
        for line in lines:
            direction = line["direction"]
            amount = int(line["amount_minor"])
            if direction not in DIRECTIONS:
                raise ValidationError(f"Invalid direction '{direction}': use debit or credit")
            if amount < 0:
                raise ValidationError("Line amounts must be positive; use the other direction")
            debits += amount if direction == "debit" else 0
            credits += amount if direction == "credit" else 0

        if debits != credits:
            raise ValidationError(
                f"Entry is out of balance: debits {format_minor(debits)} != "
                f"credits {format_minor(credits)}"
            )

        entry_id = new_id("je")
        now = now_iso()
        entry_number = await self._sequences.next_number(conn, "journal_entry")
        await self._write.insert(
            conn,
            {
                "id": entry_id,
                "entry_number": entry_number,
                "entry_date": entry_date,
                "description": description,
                "reference": reference,
                "reverses_id": reverses_id,
                "source_type": source_type,
                "source_id": source_id,
                "created_at": now,
                "created_by": actor,
            },
        )
        for line_no, line in enumerate(lines, start=1):
            await self._write.insert_line(
                conn,
                {
                    "id": new_id("jl"),
                    "entry_id": entry_id,
                    "account_id": line["account_id"],
                    "line_no": line_no,
                    "direction": line["direction"],
                    "amount_minor": int(line["amount_minor"]),
                    "memo": line.get("memo"),
                    "created_at": now,
                },
            )
        return {
            "id": entry_id,
            "entry_number": entry_number,
            "entry_date": entry_date,
            "description": description,
            "total_minor": debits,
            "total": format_minor(debits),
        }

    async def post_adjustment(
        self,
        entry_date: str,
        description: str,
        lines: list[dict],
        reference: str | None = None,
        actor: str = "mcp",
    ) -> dict:
        """A manual journal entry — the escape hatch for anything the document
        flows do not cover (opening balances, accruals, corrections).

        Lines name accounts by id, code, or name so a caller can post without
        looking ids up first.
        """
        resolved = []
        for line in lines:
            identifier = line.get("account")
            account = await self._accounts.get(str(identifier))
            if not account:
                raise NotFoundError(f"Account not found: {identifier}")
            resolved.append(
                {
                    "account_id": account["id"],
                    "direction": line["direction"],
                    "amount_minor": int(line["amount_minor"]),
                    "memo": line.get("memo"),
                }
            )
        async with self._db.unit_of_work() as conn:
            entry = await self.post(
                conn,
                entry_date,
                description,
                resolved,
                "adjustment",
                reference=reference,
                actor=actor,
            )
            await self._audit.append(
                conn,
                {
                    "id": new_id("aud"),
                    "actor": actor,
                    "action": "post_adjustment",
                    "object_type": "journal_entry",
                    "object_id": entry["id"],
                    "details": f"{entry['entry_number']} · {description} · {entry['total']}",
                    "created_at": now_iso(),
                },
            )
        return entry

    async def reverse(
        self, entry_id: str, entry_date: str | None = None, actor: str = "mcp"
    ) -> dict:
        """Reverse a posted entry with a contra entry. The original is never
        edited — this is what makes the ledger a ledger."""
        original = await self._read.get(entry_id)
        if not original:
            raise NotFoundError(f"Journal entry not found: {entry_id}")
        if original["status"] == "reversed":
            raise ConflictError(
                f"Entry {original['entry_number']} is already reversed by "
                f"{original['reversed_by_id']}"
            )

        lines = await self._read.lines(original["id"])
        flipped = []
        for line in lines:
            account = await self._accounts.get(line["account_code"])
            flipped.append(
                {
                    "account_id": account["id"] if account else "",
                    "direction": "credit" if line["direction"] == "debit" else "debit",
                    "amount_minor": line["amount_minor"],
                    "memo": line.get("memo"),
                }
            )

        async with self._db.unit_of_work() as conn:
            contra = await self.post(
                conn,
                entry_date or original["entry_date"],
                f"Reversal of {original['entry_number']}: {original['description']}",
                flipped,
                "reversal",
                source_id=original["id"],
                reference=original.get("reference"),
                actor=actor,
                reverses_id=original["id"],
            )
            await self._write.mark_reversed(conn, original["id"], contra["id"])
            await self._audit.append(
                conn,
                {
                    "id": new_id("aud"),
                    "actor": actor,
                    "action": "reverse_entry",
                    "object_type": "journal_entry",
                    "object_id": original["id"],
                    "details": f"Reversed {original['entry_number']} via {contra['entry_number']}",
                    "created_at": now_iso(),
                },
            )
        return {"reversed": original["entry_number"], "contra_entry": contra}

    # -- reads -----------------------------------------------------------------

    async def get_entry(self, entry_id: str) -> dict | None:
        entry = await self._read.get(entry_id)
        if not entry:
            return None
        lines = await self._read.lines(entry["id"])
        entry["lines"] = [{**line, "amount": format_minor(line["amount_minor"])} for line in lines]
        entry["total_minor"] = sum(
            line["amount_minor"] for line in lines if line["direction"] == "debit"
        )
        entry["total"] = format_minor(entry["total_minor"])
        return entry

    async def search(
        self,
        q: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        source_type: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        rows, total = await self._read.search(q, date_from, date_to, source_type, limit, offset)
        items = [{**row, "total": format_minor(row["total_minor"] or 0)} for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
