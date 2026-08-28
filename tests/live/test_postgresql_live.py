from __future__ import annotations

import os
import unittest

from packages.catalog.sqlverity_catalog.repository import PostgreSQLCatalogRepository
from packages.connectors.sqlverity_connectors.connection import (
    load_secret_resolver_from_environment,
)
from packages.connectors.sqlverity_connectors.postgresql import PostgreSQLConnector
from packages.connectors.sqlverity_connectors.postgresql_executor import (
    PostgreSQLReadOnlyExecutor,
)
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
)

_SECRET_REF = os.environ.get("SQLVERITY_LIVE_POSTGRES_SECRET_REF", "")


@unittest.skipUnless(_SECRET_REF, "SQLVERITY_LIVE_POSTGRES_SECRET_REF is not configured")
class PostgreSQLLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = load_secret_resolver_from_environment()
        secret = self.resolver.resolve_postgresql(_SECRET_REF)
        self.repository = PostgreSQLCatalogRepository(
            secret.as_connect_kwargs(application_name="sqlverity-live-catalog-test")
        )
        self.repository.initialize()
        self.source = DataSource(
            tenant_id="00000000-0000-0000-0000-000000000001",
            name="Live golden fixture",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            capabilities=frozenset(
                {
                    DataSourceCapability.INTROSPECT,
                    DataSourceCapability.EXPLAIN,
                    DataSourceCapability.EXECUTE_READ_ONLY,
                }
            ),
            connection_secret_ref=_SECRET_REF,
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_catalog_migrations_and_health_are_live(self) -> None:
        self.assertTrue(self.repository.health_check())

    def test_introspection_explain_and_read_only_execution_are_live(self) -> None:
        snapshot = PostgreSQLConnector(self.resolver).introspect(self.source)
        references = {item.reference for item in snapshot.objects}
        self.assertIn("commerce.orders", references)
        executor = PostgreSQLReadOnlyExecutor(self.resolver)
        explained = executor.explain(
            self.source,
            "live-explain",
            "SELECT status, COUNT(*) FROM commerce.orders GROUP BY status",
            {},
            timeout_seconds=10,
        )
        result = executor.execute_read_only(
            self.source,
            "live-execute",
            "SELECT country, COUNT(*) AS customer_count "
            "FROM commerce.customers GROUP BY country ORDER BY country",
            {},
            timeout_seconds=10,
            max_rows=100,
            max_result_bytes=100_000,
        )

        self.assertIsNotNone(explained.plan)
        self.assertEqual(2, result.row_count)
        self.assertFalse(result.truncated)
