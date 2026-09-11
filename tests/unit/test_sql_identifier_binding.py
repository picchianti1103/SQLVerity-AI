from __future__ import annotations

import unittest

from packages.connectors.sqlverity_connectors.ddl import OracleDDLParser, PostgreSQLDDLParser
from packages.domain.sqlverity_domain.contracts import SQLProposal, ValidationResult
from packages.sql_engine.sqlverity_sql_engine import SQLValidatorRegistry


class SQLIdentifierBindingTests(unittest.TestCase):
    def validate(
        self,
        sql: str,
        *,
        tables: tuple[str, ...] = ("public.orders",),
        columns: tuple[str, ...] = ("public.orders.id",),
        dialect: str = "postgresql",
        allowed_tables: tuple[str, ...] | None = None,
        allowed_columns: tuple[str, ...] | None = None,
    ) -> ValidationResult:
        return SQLValidatorRegistry().validate(
            SQLProposal(
                intent="data_query", sql=sql, dialect=dialect, tables=tables, columns=columns
            ),
            allowed_tables=frozenset(allowed_tables or tables),
            allowed_columns=frozenset(allowed_columns or columns),
            max_rows=2,
        )

    def test_quoted_identifiers_cannot_resolve_to_different_physical_objects(self) -> None:
        for sql in (
            'SELECT "ID" FROM public.orders',
            'SELECT id FROM public."Orders"',
            'SELECT id FROM "Public".orders',
            'SELECT "O".id FROM public.orders o',
            "SELECT private.orders.id FROM public.orders",
            "SELECT external.public.orders.id FROM public.orders",
            'SELECT id FROM "public.orders"',
        ):
            with self.subTest(sql=sql):
                result = self.validate(sql)
                self.assertFalse(result.accepted, result)
                self.assertIsNone(result.normalized_sql)

    def test_unquoted_names_are_folded_and_bound_to_the_allowed_schema(self) -> None:
        for sql in (
            "SELECT ID FROM ORDERS",
            "SELECT public.orders.id FROM public.orders",
            'SELECT "id" FROM "public"."orders"',
            "SELECT ID /* sqlglot.meta case_sensitive */ FROM ORDERS",
        ):
            with self.subTest(sql=sql):
                result = self.validate(sql)
                self.assertTrue(result.accepted, result.issues)
                assert result.normalized_sql is not None
                self.assertIn('"orders"."id"', result.normalized_sql)
                self.assertIn('FROM "public"."orders"', result.normalized_sql)
                again = self.validate(result.normalized_sql)
                self.assertEqual(result.normalized_sql, again.normalized_sql)
                self.assertEqual(("public.orders.id",), result.output_lineage[0].source_columns)

    def test_case_distinct_physical_objects_keep_separate_lineage(self) -> None:
        for table, column in (("orders", "id"), ("Orders", "ID")):
            with self.subTest(table=table):
                ref = f"public.{table}"
                result = self.validate(
                    f'SELECT "{column}" FROM public."{table}"',
                    tables=(ref,),
                    columns=(f"{ref}.{column}",),
                    allowed_tables=("public.orders", "public.Orders"),
                    allowed_columns=("public.orders.id", "public.Orders.ID"),
                )
                self.assertTrue(result.accepted, result.issues)
                self.assertEqual((f"{ref}.{column}",), result.output_lineage[0].source_columns)

    def test_ambiguous_schema_and_self_join_are_rejected(self) -> None:
        result = self.validate(
            "SELECT id FROM orders", allowed_tables=("public.orders", "private.orders")
        )
        self.assertIn("ambiguous_table", {issue.code for issue in result.issues})
        result = self.validate("SELECT id FROM orders o JOIN orders p ON o.id = p.id")
        self.assertIn("ambiguous_column", {issue.code for issue in result.issues})

    def test_quoted_cte_and_output_aliases_keep_their_identity(self) -> None:
        result = self.validate(
            'WITH "Orders" AS (SELECT id AS "ID", customer_id AS id FROM orders) '
            'SELECT "Orders"."ID", "Orders".id FROM "Orders"',
            columns=("public.orders.id", "public.orders.customer_id"),
        )
        self.assertTrue(result.accepted, result.issues)
        self.assertTrue(result.output_lineage_complete)
        self.assertEqual(("public.orders.id",), result.output_lineage[0].source_columns)
        self.assertEqual(("public.orders.customer_id",), result.output_lineage[1].source_columns)

    def test_cte_column_list_and_correlated_subquery_are_bound(self) -> None:
        cte = self.validate('WITH recent("ID") AS (SELECT id FROM orders) SELECT "ID" FROM recent')
        self.assertTrue(cte.accepted, cte.issues)
        self.assertEqual(("public.orders.id",), cte.output_lineage[0].source_columns)
        correlated = self.validate(
            'SELECT "O".id FROM orders "O" '
            'WHERE EXISTS (SELECT 1 FROM orders p WHERE p.id = "O".id)',
        )
        self.assertTrue(correlated.accepted, correlated.issues)
        assert correlated.normalized_sql is not None
        self.assertEqual(2, correlated.normalized_sql.count('"public"."orders"'))

    def test_oracle_folding_and_exact_spelling_for_configurable_dialects(self) -> None:
        result = self.validate(
            "SELECT id FROM sales.orders",
            dialect="oracle",
            tables=("SALES.ORDERS",),
            columns=("SALES.ORDERS.ID",),
        )
        self.assertTrue(result.accepted, result.issues)
        result = self.validate(
            'SELECT "id" FROM sales.orders',
            dialect="oracle",
            tables=("SALES.ORDERS",),
            columns=("SALES.ORDERS.ID",),
        )
        self.assertFalse(result.accepted)
        for dialect in ("mysql", "mariadb", "sqlserver"):
            with self.subTest(dialect=dialect):
                result = self.validate("SELECT id FROM public.Orders", dialect=dialect)
                self.assertFalse(result.accepted)

    def test_ddl_import_records_physical_case_instead_of_query_spelling(self) -> None:
        for parser, schema, table, column in (
            (PostgreSQLDDLParser(), "public", "orders", "id"),
            (OracleDDLParser(), "PUBLIC", "ORDERS", "ID"),
        ):
            with self.subTest(dialect=type(parser).__name__):
                snapshot = parser.parse(
                    data_source_id="source",
                    ddl=('CREATE TABLE Public.Orders (Id INTEGER, "Id" INTEGER);'),
                )
                obj = snapshot.objects[0]
                self.assertEqual(f"{schema}.{table}", obj.reference)
                self.assertEqual((column, "Id"), tuple(item.name for item in obj.columns))

    def test_derived_column_case_must_resolve_exactly(self) -> None:
        result = self.validate(
            'WITH recent AS (SELECT id AS "ID" FROM orders) SELECT id FROM recent',
        )
        self.assertFalse(result.accepted)
        self.assertIn("unresolved_derived_column", {issue.code for issue in result.issues})
