"""The ledger invariants. If these break, nothing else in the system is trustworthy."""

import pytest

from src.domain.errors import ConflictError, NotFoundError, ValidationError


async def test_seeded_books_are_balanced(erp):
    trial = await erp.reports.trial_balance()
    assert trial["balanced"], trial
    assert trial["difference_minor"] == 0


async def test_balance_sheet_balances(erp):
    sheet = await erp.reports.balance_sheet()
    assert sheet["balanced"], sheet
    assert sheet["difference_minor"] == 0


async def test_unbalanced_entry_is_refused(erp):
    with pytest.raises(ValidationError, match="out of balance"):
        await erp.posting.post_adjustment(
            "2026-01-15",
            "Deliberately lopsided",
            [
                {"account": "1010", "direction": "debit", "amount_minor": 5000},
                {"account": "3000", "direction": "credit", "amount_minor": 4000},
            ],
        )


async def test_single_line_entry_is_refused(erp):
    with pytest.raises(ValidationError, match="at least 2 lines"):
        await erp.posting.post_adjustment(
            "2026-01-15",
            "Half an entry",
            [{"account": "1010", "direction": "debit", "amount_minor": 5000}],
        )


async def test_negative_amount_is_refused(erp):
    with pytest.raises(ValidationError, match="positive"):
        await erp.posting.post_adjustment(
            "2026-01-15",
            "Negative line",
            [
                {"account": "1010", "direction": "debit", "amount_minor": -5000},
                {"account": "3000", "direction": "credit", "amount_minor": -5000},
            ],
        )


async def test_unknown_account_is_refused(erp):
    with pytest.raises(NotFoundError):
        await erp.posting.post_adjustment(
            "2026-01-15",
            "Nowhere to post",
            [
                {"account": "9999", "direction": "debit", "amount_minor": 100},
                {"account": "3000", "direction": "credit", "amount_minor": 100},
            ],
        )


async def test_reversal_leaves_the_books_balanced(erp):
    entry = await erp.posting.post_adjustment(
        "2026-02-01",
        "Accrue rent",
        [
            {"account": "6000", "direction": "debit", "amount_minor": 120_000},
            {"account": "2000", "direction": "credit", "amount_minor": 120_000},
        ],
    )
    before = await erp.accounts.balance("6000")
    reversal = await erp.posting.reverse(entry["entry_number"])
    after = await erp.accounts.balance("6000")

    assert reversal["contra_entry"]["total_minor"] == entry["total_minor"]
    assert after["balance_minor"] == before["balance_minor"] - 120_000
    assert (await erp.reports.trial_balance())["balanced"]


async def test_an_entry_reverses_only_once(erp):
    entry = await erp.posting.post_adjustment(
        "2026-02-02",
        "Reverse me twice",
        [
            {"account": "6000", "direction": "debit", "amount_minor": 500},
            {"account": "2000", "direction": "credit", "amount_minor": 500},
        ],
    )
    await erp.posting.reverse(entry["id"])
    with pytest.raises(ConflictError, match="already reversed"):
        await erp.posting.reverse(entry["id"])


async def test_every_posting_names_its_source_document(erp):
    entries = await erp.posting.search(source_type="goods_receipt", limit=50)
    assert entries["total"] >= 1
    for item in entries["items"]:
        assert item["source_id"], "a receipt posting must point back at its GRN"
