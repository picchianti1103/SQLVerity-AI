from __future__ import annotations

import tempfile
from pathlib import Path

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import ExecutionCostPolicy
from tests import query_approval_cases


class SQLiteQueryApprovalTests(query_approval_cases.QueryApprovalCases):
    def repositories(self) -> tuple[SQLiteCatalogRepository, SQLiteCatalogRepository]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "approval.sqlite3"
        return SQLiteCatalogRepository(path), SQLiteCatalogRepository(path)

    def test_existing_sqlite_catalog_gains_policy_revisions_without_losing_tickets(self) -> None:
        self.repository.upsert_execution_cost_policy(ExecutionCostPolicy(
            tenant_id=self.args["tenant_id"], data_source_id=self.args["data_source_id"],
            max_total_cost=100,
        ))
        with self.repository._connection:
            self.repository._connection.execute(
                "ALTER TABLE query_requests DROP COLUMN approved_cost_policy_revision",
            )
            self.repository._connection.execute(
                "ALTER TABLE execution_cost_policies DROP COLUMN revision",
            )
        self.repository.initialize()
        self.assertIsNone(self.current().approved_cost_policy_revision)
        policy = self.repository.get_execution_cost_policy(
            self.args["tenant_id"], self.args["data_source_id"],
        )
        assert policy is not None
        self.assertEqual(1, policy.revision)
        self.assertEqual(100, policy.max_total_cost)
