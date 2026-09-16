# Contributing to OpenERP

Thanks for helping. This project has strong opinions about correctness; the
notes below are mostly about keeping them intact.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python scripts/seed.py
```

## Before you open a PR

```bash
ruff check .
ruff format --check .
mypy . --ignore-missing-imports
pytest -q
python scripts/seed.py && python scripts/smoke_test.py
```

CI runs exactly this on Python 3.11 and 3.12, plus a Docker build.

## The rules that are not negotiable

1. **Nothing writes to the ledger except `PostingService.post`.** If a new
   document needs to post, call it — do not insert into `journal_lines`.
2. **No floats.** Money is integer minor units, quantity integer milli-units.
   Use the helpers in `src/money.py`; if you need a new division, add it there
   with half-up integer rounding rather than dividing at the call site.
3. **SQL lives in `repositories/sqlite.py` only.** Services depend on the
   protocols in `repositories/protocols.py`. If a service needs new data, add a
   method to the protocol and implement it, rather than reaching for a query.
4. **One document, one Unit of Work.** A document, its stock moves, its journal
   entry and its audit row commit together or not at all.
5. **Every mutation writes an audit row**, inside the same transaction.
6. **Postings are immutable.** Corrections are contra entries. Do not add an
   update path to `journal_entries` or `journal_lines`.

## Adding a tool

Tools in `src/mcp_server.py` hold no business logic — they call a service and
serialize. Write the use case in a service, cover it in `tests/`, then add the
thin tool. Docstrings are the agent-facing documentation: say what the tool does
and give an example question it answers.

## Tests

New behaviour needs a test that would fail without it. Invariant tests are more
valuable here than coverage: the most useful test in the suite asserts that
stock valuation equals the Inventory account balance.
