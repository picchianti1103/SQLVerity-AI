from __future__ import annotations

import unittest

from packages.connectors.sqlverity_connectors.ddl import (
    MariaDBDDLParser,
    MySQLDDLParser,
    OracleDDLParser,
    SQLServerDDLParser,
)
from packages.domain.sqlverity_domain.contracts import SQLProposal
from packages.domain.sqlverity_domain.models import ObjectKind
from packages.sql_engine.sqlverity_sql_engine import (
    DEFAULT_DIALECT_REGISTRY,
    MariaDBSQLValidator,
    MySQLSQLValidator,
    OracleSQLValidator,
    SQLServerSQLValidator,
    SQLValidatorRegistry,
    UnsupportedDialectError,
)


class DialectRegistryTests(unittest.TestCase):
    def test_resolves_canonical_dialects_and_postgres_alias(self) -> None:
        self.assertEqual("postgresql", DEFAULT_DIALECT_REGISTRY.resolve("postgres").dialect)
        self.assertEqual("mysql", DEFAULT_DIALECT_REGISTRY.resolve("MySQL").sqlglot_name)
        self.assertEqual("mysql", DEFAULT_DIALECT_REGISTRY.resolve("mariadb").sqlglot_name)
        self.assertEqual(
            ("mariadb", "mysql", "oracle", "postgresql", "sqlserver"),
            DEFAULT_DIALECT_REGISTRY.dialects,
        )
        self.assertEqual("oracle", DEFAULT_DIALECT_REGISTRY.resolve("oracle").sqlglot_name)
        self.assertEqual("tsql", DEFAULT_DIALECT_REGISTRY.resolve("mssql").sqlglot_name)

        with self.assertRaises(UnsupportedDialectError):
            DEFAULT_DIALECT_REGISTRY.resolve("snowflake")


class MySQLSQLValidatorTests(unittest.TestCase):
    allowed_tables = frozenset({"analytics.orders", "analytics.customers"})
    allowed_columns = frozenset(
        {
            "analytics.orders.id",
            "analytics.orders.customer_id",
            "analytics.customers.id",
            "analytics.customers.name",
        }
    )

    def test_mysql_backticks_join_and_limit_are_validated(self) -> None:
        result = MySQLSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql=(
                    "SELECT o.`id`, c.`name` FROM `analytics`.`orders` o "
                    "JOIN `analytics`.`customers` c ON c.`id` = o.`customer_id`"
                ),
                dialect="mysql",
                tables=("analytics.orders", "analytics.customers"),
                columns=(
                    "analytics.orders.id",
                    "analytics.customers.name",
                    "analytics.customers.id",
                    "analytics.orders.customer_id",
                ),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=250,
        )

        self.assertTrue(result.accepted, result.issues)
        self.assertEqual("mysql", result.dialect)
        assert result.normalized_sql is not None
        self.assertIn("LIMIT 250", result.normalized_sql)

    def test_mysql_dangerous_function_and_cross_database_are_rejected(self) -> None:
        dangerous = MySQLSQLValidator().validate(
            SQLProposal(intent="data_query", sql="SELECT SLEEP(1)", dialect="mysql"),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=100,
        )
        cross_database = MySQLSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT id FROM external.analytics.orders",
                dialect="mysql",
                tables=("analytics.orders",),
                columns=("analytics.orders.id",),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=100,
        )

        self.assertIn("dangerous_function", {issue.code for issue in dangerous.issues})
        self.assertIn(
            "cross_catalog_reference",
            {issue.code for issue in cross_database.issues},
        )

    def test_registry_routes_mariadb_without_postgres_fallback(self) -> None:
        result = SQLValidatorRegistry().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT id FROM analytics.orders",
                dialect="mariadb",
                tables=("analytics.orders",),
                columns=("analytics.orders.id",),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=100,
        )
        unsupported = SQLValidatorRegistry().validate(
            SQLProposal(intent="data_query", sql="SELECT 1", dialect="snowflake"),
            allowed_tables=frozenset(),
            allowed_columns=frozenset(),
            max_rows=100,
        )

        self.assertTrue(result.accepted, result.issues)
        self.assertEqual("mariadb", result.dialect)
        self.assertEqual("unsupported_dialect", unsupported.issues[0].code)
        self.assertFalse(unsupported.accepted)
        self.assertIsInstance(MariaDBSQLValidator(), MariaDBSQLValidator)


class EnterpriseDialectValidatorTests(unittest.TestCase):
    allowed_tables = frozenset({"sales.orders"})
    allowed_columns = frozenset({"sales.orders.id", "sales.orders.created_at"})

    def test_oracle_query_uses_fetch_first_limit(self) -> None:
        result = OracleSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql='SELECT o."ID" FROM "SALES"."ORDERS" o',
                dialect="oracle",
                tables=("sales.orders",),
                columns=("sales.orders.id",),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=75,
        )

        self.assertTrue(result.accepted, result.issues)
        assert result.normalized_sql is not None
        self.assertIn("FETCH FIRST 75 ROWS ONLY", result.normalized_sql)
        dangerous = OracleSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT DBMS_LOCK.SLEEP(1) FROM dual",
                dialect="oracle",
            ),
            allowed_tables=frozenset(),
            allowed_columns=frozenset(),
            max_rows=75,
        )
        self.assertIn("dangerous_function", {issue.code for issue in dangerous.issues})

    def test_sqlserver_alias_uses_top_and_rejects_cross_database(self) -> None:
        accepted = SQLServerSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT o.[id] FROM [sales].[orders] o",
                dialect="mssql",
                tables=("sales.orders",),
                columns=("sales.orders.id",),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=40,
        )
        rejected = SQLServerSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT id FROM otherdb.sales.orders",
                dialect="sqlserver",
                tables=("sales.orders",),
                columns=("sales.orders.id",),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=40,
        )
        dangerous = SQLServerSQLValidator().validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT OPENROWSET(a, b, c)",
                dialect="sqlserver",
            ),
            allowed_tables=frozenset(),
            allowed_columns=frozenset(),
            max_rows=40,
        )

        self.assertTrue(accepted.accepted, accepted.issues)
        assert accepted.normalized_sql is not None
        self.assertIn("TOP 40", accepted.normalized_sql)
        self.assertIn(
            "cross_catalog_reference",
            {issue.code for issue in rejected.issues},
        )
        self.assertIn("dangerous_function", {issue.code for issue in dangerous.issues})


MYSQL_DDL = """
CREATE TABLE customers (
    id BIGINT PRIMARY KEY,
    name VARCHAR(100) NOT NULL COMMENT 'Customer name'
) COMMENT='Registered customers';

CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    amount DECIMAL(18, 2) DEFAULT 0,
    CONSTRAINT orders_customer_fk
        FOREIGN KEY (customer_id) REFERENCES customers (id)
);

CREATE VIEW order_totals AS
SELECT customer_id, SUM(amount) AS total
FROM orders
GROUP BY customer_id;
"""


class MySQLDDLParserTests(unittest.TestCase):
    def test_mysql_parser_preserves_types_keys_views_and_inline_comments(self) -> None:
        snapshot = MySQLDDLParser().parse(
            data_source_id="source-1",
            ddl=MYSQL_DDL,
            default_schema="analytics",
        )

        self.assertEqual("mysql", snapshot.dialect)
        self.assertEqual(3, len(snapshot.objects))
        customers = next(item for item in snapshot.objects if item.name == "customers")
        orders = next(item for item in snapshot.objects if item.name == "orders")
        view = next(item for item in snapshot.objects if item.name == "order_totals")
        self.assertEqual("Registered customers", customers.comment)
        self.assertEqual("Customer name", customers.columns[1].comment)
        self.assertTrue(orders.columns[0].is_primary_key)
        self.assertEqual("DECIMAL(18, 2)", orders.columns[2].physical_type)
        self.assertEqual(ObjectKind.VIEW, view.kind)
        self.assertEqual("analytics.customers", snapshot.relationships[0].target_object_ref)

    def test_mariadb_parser_emits_mariadb_snapshot(self) -> None:
        snapshot = MariaDBDDLParser().parse(
            data_source_id="source-1",
            ddl="CREATE TABLE events (id BIGINT PRIMARY KEY)",
            default_schema="analytics",
        )

        self.assertEqual("mariadb", snapshot.dialect)


class EnterpriseDDLParserTests(unittest.TestCase):
    def test_oracle_table_and_comment_are_imported(self) -> None:
        snapshot = OracleDDLParser().parse(
            data_source_id="source-1",
            ddl=(
                "CREATE TABLE orders (id NUMBER(19) PRIMARY KEY, created_at TIMESTAMP);"
                "COMMENT ON TABLE orders IS 'Orders';"
            ),
            default_schema="SALES",
        )

        self.assertEqual("oracle", snapshot.dialect)
        self.assertEqual("Orders", snapshot.objects[0].comment)
        self.assertTrue(snapshot.objects[0].columns[0].is_primary_key)

    def test_sqlserver_table_is_imported_with_tsql_types(self) -> None:
        snapshot = SQLServerDDLParser().parse(
            data_source_id="source-1",
            ddl="CREATE TABLE sales.orders (id BIGINT PRIMARY KEY, label NVARCHAR(100));",
            default_schema="dbo",
        )

        self.assertEqual("sqlserver", snapshot.dialect)
        self.assertEqual("sales", snapshot.objects[0].schema_name)
        self.assertEqual("NVARCHAR(100)", snapshot.objects[0].columns[1].physical_type)


if __name__ == "__main__":
    unittest.main()
