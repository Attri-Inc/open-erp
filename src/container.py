"""Composition root — the only place concrete implementations are chosen and wired.

Everything else depends on protocols. To swap SQLite for Postgres, you implement
the repository protocols and change this file; no service or tool changes.
"""

from src.config import DB_PATH
from src.infrastructure.database import Database
from src.repositories.sqlite import (
    SqliteAccountRepository,
    SqliteAuditRepository,
    SqliteItemRepository,
    SqliteJournalRepository,
    SqlitePartnerRepository,
    SqlitePaymentRepository,
    SqlitePurchaseRepository,
    SqliteQueryRepository,
    SqliteSalesRepository,
    SqliteSequenceRepository,
    SqliteSettingsRepository,
    SqliteStockRepository,
    SqliteWarehouseRepository,
)
from src.services.audit import AuditService
from src.services.inventory import InventoryService
from src.services.masters import (
    AccountService,
    ItemService,
    PartnerService,
    WarehouseService,
)
from src.services.posting import PostingService
from src.services.purchasing import PurchasingService
from src.services.query import QueryService
from src.services.reports import ReportService
from src.services.sales import SalesService


class Container:
    def __init__(self, db_path: str = DB_PATH):
        self.database = Database(db_path)

        partners_repo = SqlitePartnerRepository(self.database)
        items_repo = SqliteItemRepository(self.database)
        warehouses_repo = SqliteWarehouseRepository(self.database)
        stock_repo = SqliteStockRepository(self.database)
        accounts_repo = SqliteAccountRepository(self.database)
        journal_repo = SqliteJournalRepository(self.database)
        purchase_repo = SqlitePurchaseRepository(self.database)
        sales_repo = SqliteSalesRepository(self.database)
        payments_repo = SqlitePaymentRepository(self.database)
        sequences_repo = SqliteSequenceRepository(self.database)
        settings_repo = SqliteSettingsRepository(self.database)
        audit_repo = SqliteAuditRepository(self.database)
        query_repo = SqliteQueryRepository(self.database)

        self.partners = PartnerService(self.database, partners_repo, partners_repo, audit_repo)
        self.items = ItemService(self.database, items_repo, items_repo, audit_repo)
        self.warehouses = WarehouseService(
            self.database, warehouses_repo, warehouses_repo, audit_repo
        )
        self.accounts = AccountService(self.database, accounts_repo, accounts_repo, audit_repo)

        self.posting = PostingService(
            self.database, accounts_repo, journal_repo, journal_repo, sequences_repo, audit_repo
        )
        self.inventory = InventoryService(
            self.database,
            self.items,
            self.warehouses,
            items_repo,
            items_repo,
            stock_repo,
            stock_repo,
            self.posting,
            audit_repo,
        )
        self.purchasing = PurchasingService(
            self.database,
            purchase_repo,
            purchase_repo,
            self.partners,
            self.items,
            self.warehouses,
            self.inventory,
            self.posting,
            payments_repo,
            sequences_repo,
            settings_repo,
            audit_repo,
        )
        self.sales = SalesService(
            self.database,
            sales_repo,
            sales_repo,
            self.partners,
            self.items,
            self.warehouses,
            self.inventory,
            self.posting,
            payments_repo,
            sequences_repo,
            audit_repo,
        )
        self.reports = ReportService(accounts_repo, purchase_repo, sales_repo)
        self.audit = AuditService(audit_repo)
        self.queries = QueryService(query_repo)

    async def close(self) -> None:
        await self.database.close()


# Process-wide singleton used by the MCP server.
container = Container()
