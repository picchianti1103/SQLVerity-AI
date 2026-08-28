from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import (
    ConnectorConfigurationError,
    ConnectorUnavailableError,
    EnvironmentSecretResolver,
    PostgreSQLConnectionSecret,
    SecretResolutionError,
)
from packages.connectors.sqlverity_connectors.postgresql import PostgreSQLConnector
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
    ObjectKind,
)


@dataclass(frozen=True)
class Description:
    name: str


class FakeCursor:
    def __init__(self) -> None:
        self.description: tuple[Description, ...] = ()
        self._rows: list[tuple[Any, ...]] = []
        self.statements: list[str] = []

    def execute(self, query: str) -> None:
        self.statements.append(query)
        normalized = " ".join(query.split())
        if normalized == "SET TRANSACTION READ ONLY":
            self.description = ()
            self._rows = []
        elif "obj_description" in query:
            self._set_result(
                ("schema_name", "object_name", "relation_kind", "definition_sql", "comment"),
                [
                    ("public", "customers", "r", None, "Registered customers"),
                    ("public", "orders", "r", None, "Sales orders"),
                    (
                        "reporting",
                        "order_totals",
                        "v",
                        "SELECT customer_id, sum(total_amount) FROM public.orders GROUP BY 1",
                        None,
                    ),
                ],
            )
        elif "col_description" in query:
            self._set_result(
                (
                    "schema_name",
                    "object_name",
                    "column_name",
                    "physical_type",
                    "ordinal",
                    "nullable",
                    "default_expression",
                    "comment",
                ),
                [
                    ("public", "customers", "id", "bigint", 1, False, None, "Customer key"),
                    ("public", "orders", "id", "bigint", 1, False, None, "Order key"),
                    ("public", "orders", "customer_id", "bigint", 2, False, None, None),
                    (
                        "public",
                        "orders",
                        "total_amount",
                        "numeric(18,2)",
                        3,
                        False,
                        "0",
                        "Gross order amount",
                    ),
                    (
                        "reporting",
                        "order_totals",
                        "customer_id",
                        "bigint",
                        1,
                        True,
                        None,
                        None,
                    ),
                    (
                        "reporting",
                        "order_totals",
                        "sum",
                        "numeric",
                        2,
                        True,
                        None,
                        None,
                    ),
                ],
            )
        elif "constraint_row.contype = 'p'" in query:
            self._set_result(
                ("schema_name", "object_name", "constraint_name", "column_names"),
                [
                    ("public", "customers", "customers_pkey", ["id"]),
                    ("public", "orders", "orders_pkey", ["id"]),
                ],
            )
        elif "constraint_row.contype = 'f'" in query:
            self._set_result(
                (
                    "source_schema",
                    "source_object",
                    "target_schema",
                    "target_object",
                    "constraint_name",
                    "source_columns",
                    "target_columns",
                ),
                [
                    (
                        "public",
                        "orders",
                        "public",
                        "customers",
                        "orders_customer_id_fkey",
                        ["customer_id"],
                        ["id"],
                    )
                ],
            )
        else:
            raise AssertionError(f"Unexpected SQL: {query}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def _set_result(self, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.description = tuple(Description(column) for column in columns)
        self._rows = rows


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeConnectFactory:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> FakeConnection:
        self.kwargs = kwargs
        return self.connection


class FakeSecretResolver:
    def __init__(self) -> None:
        self.last_reference: str | None = None

    def resolve_postgresql(self, secret_ref: str) -> PostgreSQLConnectionSecret:
        self.last_reference = secret_ref
        return PostgreSQLConnectionSecret(
            host="db.internal",
            database="analytics",
            username="sqlverity_reader",
            password="do-not-log-me",
        )


def connected_data_source() -> DataSource:
    return DataSource(
        tenant_id="tenant-1",
        name="Analytics",
        source_type=DataSourceType.DIRECT_DB,
        dialect="postgresql",
        capabilities=frozenset({DataSourceCapability.INTROSPECT}),
        connection_secret_ref="vault://tenant-1/analytics",
    )


class PostgreSQLConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret_resolver = FakeSecretResolver()
        self.connect_factory = FakeConnectFactory()
        self.connector = PostgreSQLConnector(self.secret_resolver, self.connect_factory)

    def test_introspects_tables_views_columns_keys_and_comments(self) -> None:
        snapshot = self.connector.introspect(connected_data_source())

        self.assertEqual("postgresql", snapshot.dialect)
        self.assertEqual(3, len(snapshot.objects))
        orders = next(item for item in snapshot.objects if item.reference == "public.orders")
        view = next(item for item in snapshot.objects if item.reference == "reporting.order_totals")
        self.assertEqual(ObjectKind.TABLE, orders.kind)
        self.assertEqual(ObjectKind.VIEW, view.kind)
        assert view.definition_sql is not None
        self.assertIn("SELECT customer_id", view.definition_sql)
        self.assertTrue(orders.columns[0].is_primary_key)
        self.assertEqual("numeric(18,2)", orders.columns[2].physical_type)
        self.assertEqual("Gross order amount", orders.columns[2].comment)
        self.assertEqual(1, len(snapshot.relationships))
        self.assertEqual(("customer_id",), snapshot.relationships[0].source_columns)

        self.assertEqual("vault://tenant-1/analytics", self.secret_resolver.last_reference)
        assert self.connect_factory.kwargs is not None
        self.assertEqual("sqlverity_reader", self.connect_factory.kwargs["user"])
        self.assertIn(
            "SET TRANSACTION READ ONLY",
            self.connect_factory.connection.cursor_instance.statements,
        )
        self.assertNotIn("do-not-log-me", repr(self.secret_resolver.resolve_postgresql("ref")))

    def test_fails_closed_without_introspection_capability(self) -> None:
        data_source = DataSource(
            tenant_id="tenant-1",
            name="Analytics",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            connection_secret_ref="vault://tenant-1/analytics",
        )
        with self.assertRaises(ConnectorConfigurationError):
            self.connector.introspect(data_source)

    def test_connection_error_is_wrapped_without_leaking_details(self) -> None:
        def failing_connect_factory(**kwargs: Any) -> FakeConnection:
            raise RuntimeError(f"could not connect with password {kwargs['password']}")

        connector = PostgreSQLConnector(self.secret_resolver, failing_connect_factory)

        with self.assertRaises(ConnectorUnavailableError) as raised:
            connector.introspect(connected_data_source())

        self.assertEqual("PostgreSQL introspection failed", str(raised.exception))
        self.assertNotIn("do-not-log-me", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


class EnvironmentSecretResolverTests(unittest.TestCase):
    def test_resolves_json_without_exposing_password_in_repr(self) -> None:
        resolver = EnvironmentSecretResolver(
            {
                "SQLVERITY_ANALYTICS_DB": (
                    '{"host":"db.internal","database":"analytics",'
                    '"username":"reader","password":"secret"}'
                )
            }
        )

        secret = resolver.resolve_postgresql("env://SQLVERITY_ANALYTICS_DB")

        self.assertEqual("analytics", secret.database)
        self.assertNotIn("secret", repr(secret))

    def test_rejects_invalid_environment_reference(self) -> None:
        resolver = EnvironmentSecretResolver({})
        with self.assertRaises(SecretResolutionError):
            resolver.resolve_postgresql("env://../../PASSWORD")


if __name__ == "__main__":
    unittest.main()
