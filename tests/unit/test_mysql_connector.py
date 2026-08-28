from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import (
    EnvironmentSecretResolver,
    MySQLConnectionSecret,
)
from packages.connectors.sqlverity_connectors.mysql import MySQLConnector
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
    ObjectKind,
)


@dataclass(frozen=True)
class Description:
    name: str


class FakeMySQLCursor:
    def __init__(self) -> None:
        self.description: tuple[Description, ...] = ()
        self.rows: list[tuple[Any, ...]] = []
        self.statements: list[str] = []

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append(query)
        if query == "START TRANSACTION READ ONLY":
            self._set((), [])
        elif "information_schema.TABLES" in query:
            self._set(
                ("schema_name", "object_name", "table_type", "definition_sql", "comment"),
                [
                    ("analytics", "customers", "BASE TABLE", None, "Customers"),
                    ("analytics", "orders", "BASE TABLE", None, "Orders"),
                    ("analytics", "order_totals", "VIEW", "SELECT 1", None),
                ],
            )
        elif "information_schema.COLUMNS" in query:
            self._set(
                (
                    "schema_name",
                    "object_name",
                    "column_name",
                    "physical_type",
                    "ordinal",
                    "is_nullable",
                    "default_expression",
                    "comment",
                ),
                [
                    ("analytics", "customers", "id", "bigint", 1, "NO", None, "Key"),
                    ("analytics", "orders", "id", "bigint", 1, "NO", None, None),
                    (
                        "analytics",
                        "orders",
                        "customer_id",
                        "bigint",
                        2,
                        "NO",
                        None,
                        None,
                    ),
                    ("analytics", "order_totals", "total", "decimal(18,2)", 1, "YES", None, None),
                ],
            )
        elif "CONSTRAINT_NAME = 'PRIMARY'" in query:
            self._set(
                ("schema_name", "object_name", "column_name", "ordinal"),
                [("analytics", "customers", "id", 1), ("analytics", "orders", "id", 1)],
            )
        elif "REFERENCED_TABLE_NAME IS NOT NULL" in query:
            self._set(
                (
                    "source_schema",
                    "source_object",
                    "target_schema",
                    "target_object",
                    "constraint_name",
                    "source_column",
                    "target_column",
                    "ordinal",
                ),
                [
                    (
                        "analytics",
                        "orders",
                        "analytics",
                        "customers",
                        "orders_customer_fk",
                        "customer_id",
                        "id",
                        1,
                    )
                ],
            )
        else:
            raise AssertionError(f"Unexpected SQL: {query}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> FakeMySQLCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def _set(self, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.description = tuple(Description(column) for column in columns)
        self.rows = rows


class FakeMySQLConnection:
    connection_id = 41

    def __init__(self) -> None:
        self.cursor_instance = FakeMySQLCursor()

    def cursor(self, **kwargs: Any) -> FakeMySQLCursor:
        return self.cursor_instance

    def __enter__(self) -> FakeMySQLConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeMySQLFactory:
    def __init__(self) -> None:
        self.connection = FakeMySQLConnection()
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> FakeMySQLConnection:
        self.kwargs = kwargs
        return self.connection


class FakeMySQLSecretResolver:
    def resolve_mysql(self, secret_ref: str) -> MySQLConnectionSecret:
        return MySQLConnectionSecret(
            host="mysql.internal",
            database="analytics",
            username="reader",
            password="do-not-log",
        )


def data_source(dialect: str = "mysql") -> DataSource:
    return DataSource(
        tenant_id="tenant-1",
        name="Analytics",
        source_type=DataSourceType.DIRECT_DB,
        dialect=dialect,
        capabilities=frozenset({DataSourceCapability.INTROSPECT}),
        connection_secret_ref="vault://analytics",
    )


class MySQLConnectorTests(unittest.TestCase):
    def test_introspects_mysql_schema_read_only(self) -> None:
        factory = FakeMySQLFactory()
        connector = MySQLConnector(FakeMySQLSecretResolver(), factory)

        snapshot = connector.introspect(data_source())

        self.assertEqual("mysql", snapshot.dialect)
        self.assertEqual(3, len(snapshot.objects))
        orders = next(item for item in snapshot.objects if item.name == "orders")
        view = next(item for item in snapshot.objects if item.name == "order_totals")
        self.assertTrue(orders.columns[0].is_primary_key)
        self.assertEqual(ObjectKind.VIEW, view.kind)
        self.assertEqual(("customer_id",), snapshot.relationships[0].source_columns)
        self.assertEqual(
            "START TRANSACTION READ ONLY",
            factory.connection.cursor_instance.statements[0],
        )
        self.assertFalse(factory.kwargs["ssl_disabled"])
        self.assertNotIn("do-not-log", repr(FakeMySQLSecretResolver().resolve_mysql("ref")))

    def test_same_connector_supports_explicit_mariadb_mode(self) -> None:
        factory = FakeMySQLFactory()
        snapshot = MySQLConnector(
            FakeMySQLSecretResolver(),
            factory,
            dialect="mariadb",
        ).introspect(data_source("mariadb"))

        self.assertEqual("mariadb", snapshot.dialect)
        self.assertTrue(factory.kwargs["ssl"])
        self.assertNotIn("ssl_disabled", factory.kwargs)

    def test_environment_resolver_builds_tls_mysql_secret(self) -> None:
        resolver = EnvironmentSecretResolver(
            {
                "SQLVERITY_MYSQL": (
                    '{"host":"mysql.internal","database":"analytics",'
                    '"username":"reader","password":"secret","ssl_ca":"ca.pem"}'
                )
            }
        )

        secret = resolver.resolve_mysql("env://SQLVERITY_MYSQL")
        kwargs = secret.as_connect_kwargs()

        self.assertEqual(3306, secret.port)
        self.assertTrue(kwargs["ssl_verify_cert"])
        self.assertTrue(kwargs["ssl_verify_identity"])
        self.assertNotIn("secret", repr(secret))


if __name__ == "__main__":
    unittest.main()
