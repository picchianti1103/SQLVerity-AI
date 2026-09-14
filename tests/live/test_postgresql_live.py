from __future__ import annotations

import os
import unittest
from typing import Any

from packages.catalog.sqlverity_catalog.repository import PostgreSQLCatalogRepository
from packages.connectors.sqlverity_connectors.connection import (
    load_secret_resolver_from_environment,
)
from packages.connectors.sqlverity_connectors.postgresql import PostgreSQLConnector
from packages.connectors.sqlverity_connectors.postgresql_executor import (
    PostgreSQLReadOnlyExecutor,
    ReadOnlyExecutionError,
)
from packages.domain.sqlverity_domain.contracts import SQLProposal
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
)
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator
from tests import budget_reservation_cases, query_approval_cases, request_lease_cases

_SECRET_REF = os.environ.get("SQLVERITY_LIVE_POSTGRES_SECRET_REF", "")


@unittest.skipUnless(_SECRET_REF, "SQLVERITY_LIVE_POSTGRES_SECRET_REF is not configured")
class PostgreSQLBudgetReservationTests(budget_reservation_cases.BudgetReservationCases):
    def repositories(self) -> tuple[PostgreSQLCatalogRepository, PostgreSQLCatalogRepository]:
        secret = load_secret_resolver_from_environment().resolve_postgresql(_SECRET_REF)
        kwargs = secret.as_connect_kwargs(application_name="sqlverity-live-budget-test")
        return PostgreSQLCatalogRepository(kwargs), PostgreSQLCatalogRepository(kwargs)


@unittest.skipUnless(_SECRET_REF, "SQLVERITY_LIVE_POSTGRES_SECRET_REF is not configured")
class PostgreSQLRequestLeaseTests(request_lease_cases.RequestLeaseCases):
    def repositories(self) -> tuple[PostgreSQLCatalogRepository, PostgreSQLCatalogRepository]:
        secret = load_secret_resolver_from_environment().resolve_postgresql(_SECRET_REF)
        kwargs = secret.as_connect_kwargs(application_name="sqlverity-live-lease-test")
        return PostgreSQLCatalogRepository(kwargs), PostgreSQLCatalogRepository(kwargs)


@unittest.skipUnless(_SECRET_REF, "SQLVERITY_LIVE_POSTGRES_SECRET_REF is not configured")
class PostgreSQLQueryApprovalTests(query_approval_cases.QueryApprovalCases):
    def repositories(self) -> tuple[PostgreSQLCatalogRepository, PostgreSQLCatalogRepository]:
        resolver = load_secret_resolver_from_environment()
        secret = resolver.resolve_postgresql(_SECRET_REF)
        kwargs = secret.as_connect_kwargs(application_name="sqlverity-live-approval-test")
        return PostgreSQLCatalogRepository(kwargs), PostgreSQLCatalogRepository(kwargs)


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

    def test_duplicate_driver_outputs_fail_instead_of_losing_values(self) -> None:
        executor = PostgreSQLReadOnlyExecutor(self.resolver)
        with self.assertRaises(ReadOnlyExecutionError):
            executor.execute_read_only(
                self.source, "duplicate-output",
                "SELECT id, id + 101 AS id FROM identity_allowed.orders", {},
                timeout_seconds=10, max_rows=2, max_result_bytes=10_000,
            )
        result = executor.execute_read_only(
            self.source, "unique-output",
            "SELECT id AS original_id, id + 101 AS shifted_id FROM identity_allowed.orders", {},
            timeout_seconds=10, max_rows=2, max_result_bytes=10_000,
        )
        self.assertEqual({"original_id": 101, "shifted_id": 202}, result.rows[0])

    def test_physical_identity_is_independent_of_case_and_search_path(self) -> None:
        import psycopg

        def connect(**kwargs: Any) -> Any:
            return psycopg.connect(
                options="-csearch_path=identity_shadow,identity_allowed", **kwargs,
            )

        executor = PostgreSQLReadOnlyExecutor(self.resolver, connect)
        for sql, table, column, expected in (
            ("SELECT ID FROM ORDERS", "identity_allowed.orders", "id", 101),
            ('SELECT "ID" FROM orders', "identity_allowed.orders", "ID", 202),
            ('SELECT id FROM "Orders"', "identity_allowed.Orders", "id", 303),
        ):
            with self.subTest(sql=sql):
                validated = PostgreSQLSQLValidator().validate(
                    SQLProposal(intent="data_query", sql=sql, dialect="postgresql",
                                tables=(table,), columns=(f"{table}.{column}",)),
                    allowed_tables=frozenset({table}),
                    allowed_columns=frozenset({f"{table}.{column}"}), max_rows=2,
                )
                self.assertTrue(validated.accepted, validated.issues)
                assert validated.normalized_sql is not None
                result = executor.execute_read_only(
                    self.source, "live-identity", validated.normalized_sql, {},
                    timeout_seconds=10, max_rows=2, max_result_bytes=10_000,
                )
                self.assertEqual(expected, result.rows[0][column])
                self.assertEqual((f"{table}.{column}",),
                                 validated.output_lineage[0].source_columns)

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
