from __future__ import annotations

import tempfile
from pathlib import Path

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from tests import budget_reservation_cases, request_lease_cases


class SQLiteBudgetReservationTests(budget_reservation_cases.BudgetReservationCases):
    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "budget.sqlite3"
        return SQLiteCatalogRepository(path), SQLiteCatalogRepository(path)


class SQLiteRequestLeaseTests(request_lease_cases.RequestLeaseCases):
    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "lease.sqlite3"
        return SQLiteCatalogRepository(path), SQLiteCatalogRepository(path)
