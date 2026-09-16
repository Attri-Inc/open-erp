"""Reporting — every figure derived from posted journal entries and documents.

No report keeps its own totals. The trial balance, P&L and balance sheet are
computed from `journal_lines` on demand, and the aging reports read the open
documents, so a report can never disagree with the ledger it describes.
"""

from src.infrastructure.identity import parse_date, today_iso
from src.money import format_minor
from src.repositories.protocols import AccountReader, PurchaseReader, SalesReader

AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


class ReportService:
    def __init__(self, accounts: AccountReader, purchases: PurchaseReader, sales: SalesReader):
        self._accounts = accounts
        self._purchases = purchases
        self._sales = sales

    # -- financial statements --------------------------------------------------

    async def trial_balance(self, as_of: str | None = None) -> dict:
        rows = await self._accounts.trial_balance_rows(as_of)
        lines = []
        total_debit = total_credit = 0
        for row in rows:
            net = int(row["net_debit_minor"])
            if net == 0:
                continue
            debit = net if net > 0 else 0
            credit = -net if net < 0 else 0
            total_debit += debit
            total_credit += credit
            lines.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "type": row["type"],
                    "debit_minor": debit,
                    "credit_minor": credit,
                    "debit": format_minor(debit),
                    "credit": format_minor(credit),
                }
            )
        return {
            "as_of": as_of or "latest",
            "lines": lines,
            "total_debit": format_minor(total_debit),
            "total_credit": format_minor(total_credit),
            # The books are balanced when these agree. If they ever do not,
            # something wrote around PostingService — treat it as an incident.
            "balanced": total_debit == total_credit,
            "difference_minor": total_debit - total_credit,
        }

    async def _signed_totals(
        self, account_type: str, date_from: str | None, date_to: str | None
    ) -> tuple[list[dict], int]:
        rows = await self._accounts.type_totals(account_type, date_from, date_to)
        lines = []
        total = 0
        for row in rows:
            net = int(row["net_debit_minor"])
            balance = net if row["normal_side"] == "debit" else -net
            if balance == 0:
                continue
            total += balance
            lines.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "amount_minor": balance,
                    "amount": format_minor(balance),
                }
            )
        return lines, total

    async def profit_and_loss(
        self, date_from: str | None = None, date_to: str | None = None
    ) -> dict:
        income, income_total = await self._signed_totals("income", date_from, date_to)
        expense, expense_total = await self._signed_totals("expense", date_from, date_to)
        net = income_total - expense_total
        return {
            "period": {"from": date_from or "inception", "to": date_to or "latest"},
            "income": income,
            "total_income": format_minor(income_total),
            "expenses": expense,
            "total_expenses": format_minor(expense_total),
            "net_profit_minor": net,
            "net_profit": format_minor(net),
        }

    async def balance_sheet(self, as_of: str | None = None) -> dict:
        assets, assets_total = await self._signed_totals("asset", None, as_of)
        liabilities, liabilities_total = await self._signed_totals("liability", None, as_of)
        equity, equity_total = await self._signed_totals("equity", None, as_of)
        _, income_total = await self._signed_totals("income", None, as_of)
        _, expense_total = await self._signed_totals("expense", None, as_of)

        # Retained earnings are not a posted balance — they are this period's
        # income less expenses, which is what makes the sheet balance.
        retained = income_total - expense_total
        equity_with_earnings = equity_total + retained
        return {
            "as_of": as_of or "latest",
            "assets": assets,
            "total_assets": format_minor(assets_total),
            "liabilities": liabilities,
            "total_liabilities": format_minor(liabilities_total),
            "equity": equity
            + [
                {
                    "code": "—",
                    "name": "Retained earnings (current)",
                    "amount_minor": retained,
                    "amount": format_minor(retained),
                }
            ],
            "total_equity": format_minor(equity_with_earnings),
            "balanced": assets_total == liabilities_total + equity_with_earnings,
            "difference_minor": assets_total - liabilities_total - equity_with_earnings,
        }

    # -- working-capital reports ----------------------------------------------

    @staticmethod
    def _age(rows: list[dict], as_of: str, partner_key: str) -> dict:
        reference = parse_date(as_of, "as_of")
        buckets = dict.fromkeys(AGING_BUCKETS, 0)
        lines = []
        total = 0
        for row in rows:
            outstanding = int(row["outstanding_minor"])
            overdue = (reference - parse_date(row["due_date"], "due_date")).days
            bucket = _bucket(overdue)
            buckets[bucket] += outstanding
            total += outstanding
            lines.append(
                {
                    "document": row.get("invoice_number") or row.get("bill_number"),
                    "partner": row[partner_key],
                    "due_date": row["due_date"],
                    "days_overdue": max(overdue, 0),
                    "bucket": bucket,
                    "outstanding_minor": outstanding,
                    "outstanding": format_minor(outstanding),
                }
            )
        return {
            "as_of": as_of,
            "lines": lines,
            "buckets": {name: format_minor(amount) for name, amount in buckets.items()},
            "total_minor": total,
            "total": format_minor(total),
        }

    async def ar_aging(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        rows = await self._sales.open_invoices(as_of)
        return self._age(rows, as_of, "customer_name")

    async def ap_aging(self, as_of: str | None = None) -> dict:
        as_of = as_of or today_iso()
        rows = await self._purchases.open_bills(as_of)
        return self._age(rows, as_of, "supplier_name")

    async def unbilled_receipts(self) -> dict:
        """What sits in GRNI: goods received that no supplier has billed for.

        This is the three-way match backlog. A GRNI balance that never clears
        means a supplier invoice is missing or a receipt was booked in error.
        """
        account = await self._accounts.get_by_role("grni")
        if not account:
            return {"grni_balance": format_minor(0), "note": "No account is mapped to 'grni'"}
        net = await self._accounts.net_debit(account["id"], None)
        balance = -net  # GRNI is a liability: credits are positive
        return {
            "account": f"{account['code']} · {account['name']}",
            "grni_balance_minor": balance,
            "grni_balance": format_minor(balance),
            "cleared": balance == 0,
        }
