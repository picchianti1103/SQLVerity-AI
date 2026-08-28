from __future__ import annotations

import unittest

from packages.domain.sqlverity_domain.contracts import SQLProposal, ValidationResult
from packages.domain.sqlverity_domain.models import QueryParameterDefinition, QueryParameterType
from packages.sql_engine.sqlverity_sql_engine import PostgreSQLSQLValidator


class PostgreSQLSQLValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PostgreSQLSQLValidator()
        self.allowed_tables = frozenset({"public.orders", "public.customers"})
        self.allowed_columns = frozenset(
            {
                "public.orders.id",
                "public.orders.customer_id",
                "public.orders.total_amount",
                "public.orders.created_at",
                "public.customers.id",
                "public.customers.name",
            }
        )

    def test_select_join_is_resolved_and_bounded(self) -> None:
        result = self._validate(
            """
            SELECT o.id, c.name
            FROM public.orders AS o
            JOIN public.customers AS c ON c.id = o.customer_id
            """,
            tables=("public.orders", "public.customers"),
            columns=(
                "public.orders.id",
                "public.customers.name",
                "public.customers.id",
                "public.orders.customer_id",
            ),
        )

        self.assertTrue(result.accepted)
        assert result.normalized_sql is not None
        self.assertTrue(result.normalized_sql.endswith("LIMIT 500"))
        self.assertEqual(
            ("public.customers", "public.orders"),
            result.referenced_tables,
        )
        self.assertIn("limit_added", self._codes(result))

    def test_large_limit_is_capped_but_small_limit_is_preserved(self) -> None:
        large = self._validate(
            "SELECT id FROM public.orders LIMIT 2000",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )
        small = self._validate(
            "SELECT id FROM public.orders LIMIT 10",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )

        self.assertTrue(large.accepted)
        self.assertEqual("SELECT id FROM public.orders LIMIT 500", large.normalized_sql)
        self.assertIn("limit_capped", self._codes(large))
        self.assertEqual("SELECT id FROM public.orders LIMIT 10", small.normalized_sql)

    def test_dynamic_limit_is_rejected_without_preview_sql(self) -> None:
        result = self._validate(
            "SELECT id FROM public.orders LIMIT (1 + 1)",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )

        self.assertFalse(result.accepted)
        self.assertIsNone(result.normalized_sql)
        self.assertIn("dynamic_limit_not_allowed", self._codes(result))

    def test_positional_parameters_are_rejected_in_favor_of_governed_names(self) -> None:
        result = self._validate(
            "SELECT id FROM public.orders WHERE customer_id = $1",
            tables=("public.orders",),
            columns=("public.orders.id", "public.orders.customer_id"),
        )

        self.assertFalse(result.accepted)
        self.assertIsNone(result.normalized_sql)
        self.assertIn("parameter_syntax_not_allowed", self._codes(result))

    def test_named_parameters_and_output_alias_lineage_are_governed(self) -> None:
        result = self.validator.validate(
            SQLProposal(
                intent="data_query",
                sql=(
                    "SELECT id AS order_id, total_amount + id AS score "
                    "FROM public.orders "
                    "WHERE customer_id = :customer_id"
                ),
                dialect="postgresql",
                tables=("public.orders",),
                columns=(
                    "public.orders.id",
                    "public.orders.total_amount",
                    "public.orders.customer_id",
                ),
                parameters=(
                    QueryParameterDefinition(
                        name="customer_id",
                        value_type=QueryParameterType.INTEGER,
                    ),
                ),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=500,
        )

        self.assertTrue(result.accepted, result.issues)
        assert result.normalized_sql is not None
        self.assertIn("%(customer_id)s", result.normalized_sql)
        self.assertTrue(result.output_lineage_complete)
        self.assertEqual("order_id", result.output_lineage[0].output_name)
        self.assertEqual(
            ("public.orders.id",),
            result.output_lineage[0].source_columns,
        )
        self.assertEqual(
            ("public.orders.id", "public.orders.total_amount"),
            result.output_lineage[1].source_columns,
        )

    def test_parameter_declarations_must_match_and_limits_remain_static(self) -> None:
        declaration = QueryParameterDefinition(
            name="customer_id",
            value_type=QueryParameterType.INTEGER,
        )
        mismatch = self.validator.validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT id FROM public.orders WHERE customer_id = :other_id",
                dialect="postgresql",
                tables=("public.orders",),
                columns=("public.orders.id", "public.orders.customer_id"),
                parameters=(declaration,),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=500,
        )
        dynamic_limit = self.validator.validate(
            SQLProposal(
                intent="data_query",
                sql="SELECT id FROM public.orders LIMIT :customer_id",
                dialect="postgresql",
                tables=("public.orders",),
                columns=("public.orders.id",),
                parameters=(declaration,),
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=500,
        )

        self.assertIn("parameter_declaration_mismatch", self._codes(mismatch))
        self.assertIn("parameter_position_not_allowed", self._codes(dynamic_limit))

    def test_write_and_administrative_statements_are_rejected(self) -> None:
        for sql in (
            "DELETE FROM public.orders",
            "UPDATE public.orders SET total_amount = 0",
            "DROP TABLE public.orders",
            "COPY public.orders TO '/tmp/orders.csv'",
        ):
            with self.subTest(sql=sql):
                result = self._validate(sql)
                self.assertFalse(result.accepted)
                self.assertIsNone(result.normalized_sql)
                self.assertIn("statement_not_allowed", self._codes(result))

    def test_data_modifying_cte_is_rejected(self) -> None:
        result = self._validate(
            """
            WITH deleted AS (
                DELETE FROM public.orders RETURNING id
            )
            SELECT id FROM deleted
            """
        )

        self.assertFalse(result.accepted)
        self.assertIn("write_or_administrative_operation", self._codes(result))

    def test_multiple_statements_select_into_and_locking_are_rejected(self) -> None:
        multiple = self._validate("SELECT id FROM public.orders; SELECT id FROM public.orders")
        select_into = self._validate(
            "SELECT id INTO order_backup FROM public.orders",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )
        locking = self._validate(
            "SELECT id FROM public.orders FOR UPDATE",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )

        self.assertIn("multiple_statements", self._codes(multiple))
        self.assertIn("select_into_not_allowed", self._codes(select_into))
        self.assertIn("locking_clause_not_allowed", self._codes(locking))

    def test_wildcard_is_rejected_but_count_star_is_allowed(self) -> None:
        wildcard = self._validate(
            "SELECT * FROM public.orders",
            tables=("public.orders",),
        )
        count = self._validate(
            "SELECT COUNT(*) FROM public.orders",
            tables=("public.orders",),
        )

        self.assertIn("wildcard_not_allowed", self._codes(wildcard))
        self.assertTrue(count.accepted)

    def test_unknown_and_dangerous_functions_are_rejected(self) -> None:
        unknown = self._validate(
            "SELECT company_udf(id) FROM public.orders",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )
        dangerous = self._validate("SELECT pg_sleep(1)")

        self.assertIn("function_not_allowlisted", self._codes(unknown))
        self.assertIn("dangerous_function", self._codes(dangerous))

    def test_unknown_table_column_and_cross_catalog_are_rejected(self) -> None:
        unknown_table = self._validate(
            "SELECT id FROM public.payments",
            tables=("public.payments",),
            columns=("public.payments.id",),
        )
        unknown_column = self._validate(
            "SELECT secret FROM public.orders",
            tables=("public.orders",),
            columns=("public.orders.secret",),
        )
        cross_catalog = self._validate(
            "SELECT id FROM external.public.orders",
            tables=("public.orders",),
            columns=("public.orders.id",),
        )

        self.assertIn("table_not_allowed", self._codes(unknown_table))
        self.assertIn("column_not_allowed", self._codes(unknown_column))
        self.assertIn("cross_catalog_reference", self._codes(cross_catalog))

    def test_cte_and_union_keep_physical_lineage(self) -> None:
        cte = self._validate(
            """
            WITH recent AS (
                SELECT id, customer_id FROM public.orders
            )
            SELECT recent.id FROM recent
            """,
            tables=("public.orders",),
            columns=("public.orders.id", "public.orders.customer_id"),
        )
        union = self._validate(
            """
            SELECT id FROM public.orders
            UNION ALL
            SELECT id FROM public.orders
            """,
            tables=("public.orders",),
            columns=("public.orders.id",),
        )

        self.assertTrue(cte.accepted, cte.issues)
        self.assertTrue(cte.output_lineage_complete)
        self.assertEqual(
            ("public.orders.id",),
            cte.output_lineage[0].source_columns,
        )
        self.assertTrue(union.accepted, union.issues)
        self.assertFalse(union.output_lineage_complete)

    def test_ambiguous_column_and_declared_reference_mismatch_are_rejected(self) -> None:
        ambiguous = self._validate(
            """
            SELECT id
            FROM public.orders
            JOIN public.customers ON customers.id = orders.customer_id
            """,
            tables=("public.orders", "public.customers"),
            columns=(
                "public.orders.id",
                "public.customers.id",
                "public.orders.customer_id",
            ),
        )
        mismatch = self._validate(
            "SELECT id FROM public.orders",
            tables=("public.orders", "public.customers"),
            columns=("public.orders.id",),
        )

        self.assertIn("ambiguous_column", self._codes(ambiguous))
        ambiguity = next(
            issue for issue in ambiguous.issues if issue.code == "ambiguous_column"
        )
        self.assertIn("public.orders.id", ambiguity.message)
        self.assertIn("public.customers.id", ambiguity.message)
        self.assertIn("declared_table_mismatch", self._codes(mismatch))

    def test_clarification_and_parse_errors_never_produce_preview_sql(self) -> None:
        clarification = self.validator.validate(
            SQLProposal(
                intent="data_query",
                sql="",
                dialect="postgresql",
                ambiguities=("Which date range?",),
                needs_clarification=True,
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=500,
        )
        invalid = self._validate("SELECT FROM")

        self.assertIn("clarification_required", self._codes(clarification))
        self.assertIn("Which date range?", clarification.issues[0].message)
        self.assertIsNone(clarification.normalized_sql)
        self.assertIn("parse_error", self._codes(invalid))

    def _validate(
        self,
        sql: str,
        *,
        tables: tuple[str, ...] = (),
        columns: tuple[str, ...] = (),
    ) -> ValidationResult:
        return self.validator.validate(
            SQLProposal(
                intent="data_query",
                sql=sql,
                dialect="postgresql",
                tables=tables,
                columns=columns,
            ),
            allowed_tables=self.allowed_tables,
            allowed_columns=self.allowed_columns,
            max_rows=500,
        )

    @staticmethod
    def _codes(result: ValidationResult) -> set[str]:
        return {issue.code for issue in result.issues}


if __name__ == "__main__":
    unittest.main()
