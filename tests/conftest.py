"""Test fixtures — point the app at a throwaway seeded database.

The env var is set at import time, before any `src.*` module (which reads
OPENERP_DB via src.config) is imported by the test modules.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TMP_DB = Path(tempfile.mkdtemp(prefix="openerp-test-")) / "openerp.db"
os.environ["OPENERP_DB"] = str(_TMP_DB)

# Make scripts/seed.py importable as `seed`.
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="session", autouse=True)
def _seed_database():
    """Seed the throwaway DB once, and close the connections on teardown.

    The container's aiosqlite connections use non-daemon worker threads; without
    closing them the interpreter hangs at exit.
    """
    import asyncio

    import seed  # reads OPENERP_DB (set above) at import time

    seed.main()
    yield
    from src.container import container

    asyncio.run(container.close())


@pytest.fixture
def erp():
    from src.container import container

    return container
