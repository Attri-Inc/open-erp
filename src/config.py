import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("OPENERP_DB", str(BASE_DIR / "data" / "openerp.db"))
MCP_PORT = int(os.getenv("MCP_PORT", "8793"))
