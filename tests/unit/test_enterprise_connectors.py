from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import (
    EnvironmentSecretResolver,
    OracleConnectionSecret,
    SecretResolutionError,
    SQLServerConnectionSecret,
)
from packages.connectors.sqlverity_connectors.oracle import OracleConnector
from packages.connectors.sqlverity_connectors.sqlserver import SQLServerConnector
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
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.description: tuple[Description, ...] = ()
        self.rows: list[tuple[Any, ...]] = []
        self.statements: list[str] = []

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append(query)
        if query == "SET TRANSACTION READ ONLY":
            self._set((), [])
        elif self.dialect == "oracle":
            self._oracle(query)
        else:
            self._sqlserver(query)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def _oracle(self, query: str) -> None:
        if "FROM USER_OBJECTS" in query:
            self._set(
                ("SCHEMA_NAME", "OBJECT_NAME", "OBJECT_TYPE", "DEFINITION_SQL", "OBJECT_COMMENT"),
                [
                    ("SALES", "CUSTOMERS", "TABLE", None, "Customers"),
                    ("SALES", "ORDERS_VIEW", "VIEW", "SELECT ID FROM ORDERS", None),
                ],
            )
        elif "FROM USER_TAB_COLUMNS" in query:
            self._set(
                (
                    "SCHEMA_NAME",
                    "OBJECT_NAME",
                    "COLUMN_NAME",
                    "DATA_TYPE",
                    "DATA_LENGTH",
                    "CHAR_LENGTH",
                    "CHAR_USED",
                    "DATA_PRECISION",
                    "DATA_SCALE",
                    "DATA_TYPE_OWNER",
                    "ORDINAL",
                    "IS_NULLABLE",
                    "DEFAULT_EXPRESSION",
                    "COLUMN_COMMENT",
                ),
                [
                    (
                        "SALES", "CUSTOMERS", "ID", "NUMBER", 22, 0, None,
                        19, 0, None, 1, "N", None, "Key",
                    ),
                    (
                        "SALES", "ORDERS_VIEW", "ID", "NUMBER", 22, 0, None,
                        19, 0, None, 1, "Y", None, None,
                    ),
                ],
            )
        elif "CONSTRAINT_TYPE = 'P'" in query:
            self._set(
                ("SCHEMA_NAME", "OBJECT_NAME", "COLUMN_NAME", "ORDINAL"),
                [("SALES", "CUSTOMERS", "ID", 1)],
            )
        elif "CONSTRAINT_TYPE = 'R'" in query:
            self._set(
                (
                    "SOURCE_SCHEMA",
                    "SOURCE_OBJECT",
                    "TARGET_SCHEMA",
                    "TARGET_OBJECT",
                    "CONSTRAINT_NAME",
                    "SOURCE_COLUMN",
                    "TARGET_COLUMN",
                    "ORDINAL",
                ),
                [],
            )
        else:
            raise AssertionError(f"Unexpected Oracle SQL: {query}")

    def _sqlserver(self, query: str) -> None:
        if "FROM sys.objects object_row" in query and "sys.columns" not in query:
            self._set(
                ("schema_name", "object_name", "object_type", "definition_sql", "object_comment"),
                [
                    ("sales", "orders", "U", None, "Orders"),
                    ("sales", "order_view", "V", "SELECT id FROM sales.orders", None),
                ],
            )
        elif "JOIN sys.columns column_row" in query:
            self._set(
                (
                    "schema_name",
                    "object_name",
                    "column_name",
                    "data_type",
                    "max_length",
                    "data_precision",
                    "data_scale",
                    "ordinal",
                    "is_nullable",
                    "default_expression",
                    "column_comment",
                ),
                [
                    ("sales", "orders", "id", "bigint", 8, 19, 0, 1, False, None, "Key"),
                    ("sales", "orders", "label", "nvarchar", 200, 0, 0, 2, True, None, None),
                    ("sales", "order_view", "id", "bigint", 8, 19, 0, 1, True, None, None),
                ],
            )
        elif "is_primary_key = 1" in query:
            self._set(
                ("schema_name", "object_name", "column_name", "ordinal"),
                [("sales", "orders", "id", 1)],
            )
        elif "FROM sys.foreign_keys" in query:
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
                [],
            )
        else:
            raise AssertionError(f"Unexpected SQL Server SQL: {query}")

    def _set(self, columns: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.description = tuple(Description(name) for name in columns)
        self.rows = rows


class FakeConnection:
    def __init__(self, dialect: str) -> None:
        self.cursor_instance = FakeCursor(dialect)
        self.attributes: list[tuple[int, object]] = []

    def cursor(self) -> FakeCursor:
        return self.cursor_instance

    def set_attr(self, attribute: int, value: object) -> None:
        self.attributes.append((attribute, value))

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeFactory:
    def __init__(self, dialect: str) -> None:
        self.connection = FakeConnection(dialect)
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> FakeConnection:
        self.kwargs = kwargs
        return self.connection


class OracleResolver:
    def resolve_oracle(self, secret_ref: str) -> OracleConnectionSecret:
        return OracleConnectionSecret(
            host="oracle.internal",
            service_name="analytics",
            username="reader",
            password="do-not-log",
        )


class SQLServerResolver:
    def resolve_sqlserver(self, secret_ref: str) -> SQLServerConnectionSecret:
        return SQLServerConnectionSecret(
            host="sqlserver.internal",
            database="analytics",
            username="reader",
            password="do-not-log",
        )


def data_source(dialect: str) -> DataSource:
    return DataSource(
        tenant_id="tenant-1",
        name="Analytics",
        source_type=DataSourceType.DIRECT_DB,
        dialect=dialect,
        capabilities=frozenset({DataSourceCapability.INTROSPECT}),
        connection_secret_ref="vault://analytics",
    )


class EnterpriseConnectorTests(unittest.TestCase):
    def test_oracle_introspection_is_read_only_and_uses_tcps(self) -> None:
        factory = FakeFactory("oracle")

        snapshot = OracleConnector(OracleResolver(), factory).introspect(
            data_source("oracle")
        )

        self.assertEqual("oracle", snapshot.dialect)
        self.assertEqual(ObjectKind.VIEW, snapshot.objects[1].kind)
        self.assertTrue(snapshot.objects[0].columns[0].is_primary_key)
        self.assertEqual("NUMBER(19,0)", snapshot.objects[0].columns[0].physical_type)
        self.assertEqual(
            "SET TRANSACTION READ ONLY",
            factory.connection.cursor_instance.statements[0],
        )
        self.assertEqual("tcps", factory.kwargs["protocol"])
        self.assertIs(True, factory.kwargs["ssl_server_dn_match"])
        self.assertNotIn("do-not-log", repr(OracleResolver().resolve_oracle("ref")))

    def test_sqlserver_introspection_enforces_driver_read_only_mode(self) -> None:
        factory = FakeFactory("sqlserver")

        snapshot = SQLServerConnector(SQLServerResolver(), factory).introspect(
            data_source("mssql")
        )

        self.assertEqual("sqlserver", snapshot.dialect)
        self.assertEqual(ObjectKind.VIEW, snapshot.objects[1].kind)
        self.assertTrue(snapshot.objects[0].columns[0].is_primary_key)
        self.assertEqual("nvarchar(100)", snapshot.objects[0].columns[1].physical_type)
        self.assertEqual([(101, 1)], factory.connection.attributes)
        self.assertEqual("ReadOnly", factory.kwargs["applicationintent"])
        self.assertEqual("yes", factory.kwargs["encrypt"])
        self.assertEqual("no", factory.kwargs["trust_server_certificate"])
        self.assertNotIn("do-not-log", repr(SQLServerResolver().resolve_sqlserver("ref")))

    def test_environment_resolver_parses_enterprise_secrets_and_rejects_insecure_sqlserver(
        self,
    ) -> None:
        resolver = EnvironmentSecretResolver(
            {
                "ORACLE_DB": (
                    '{"host":"oracle.internal","service_name":"analytics",'
                    '"username":"reader","password":"secret"}'
                ),
                "MSSQL_DB": (
                    '{"host":"sqlserver.internal","database":"analytics",'
                    '"username":"reader","password":"secret"}'
                ),
                "MSSQL_INSECURE": (
                    '{"host":"sqlserver.internal","database":"analytics",'
                    '"username":"reader","password":"secret","encrypt":false}'
                ),
            }
        )

        oracle = resolver.resolve_oracle("env://ORACLE_DB")
        sqlserver = resolver.resolve_sqlserver("env://MSSQL_DB")

        self.assertEqual(1521, oracle.port)
        self.assertEqual(1433, sqlserver.port)
        self.assertEqual("sqlserver.internal,1433", sqlserver.as_connect_kwargs()["server"])
        with self.assertRaises(SecretResolutionError):
            resolver.resolve_sqlserver("env://MSSQL_INSECURE")


if __name__ == "__main__":
    unittest.main()
