from __future__ import annotations

import unittest
from unittest.mock import Mock

from packages.connectors.sqlverity_connectors.postgresql_executor import (
    ReadOnlyExecutionError,
    _column_names,
    _fetch_bounded_rows,
)
from packages.domain.sqlverity_domain.contracts import SQLProposal
from packages.sql_engine.sqlverity_sql_engine import SQLValidatorRegistry


class DuplicateOutputNamesTests(unittest.TestCase):
    def test_duplicate_projection_and_cte_names_are_rejected_in_all_dialects(self) -> None:
        registry = SQLValidatorRegistry()
        for dialect in ("postgresql", "mysql", "mariadb", "oracle", "sqlserver"):
            for body in (
                "SELECT id, id + 101 AS id FROM public.orders",
                "SELECT id AS value, id AS value FROM public.orders",
                "WITH c AS (SELECT id AS value, id AS value FROM public.orders) "
                "SELECT value FROM c",
                "SELECT SUM(id), MAX(id) FROM public.orders",
                "SELECT 1, 2 FROM public.orders",
            ):
                with self.subTest(dialect=dialect, sql=body):
                    table, column = (
                        ("PUBLIC.ORDERS", "PUBLIC.ORDERS.ID")
                        if dialect == "oracle"
                        else (
                            "public.orders",
                            "public.orders.id",
                        )
                    )
                    result = registry.validate(
                        SQLProposal(
                            intent="data_query",
                            dialect=dialect,
                            sql=body,
                            tables=(table,),
                            columns=(column,),
                        ),
                        allowed_tables=frozenset({table}),
                        allowed_columns=frozenset({column}),
                        max_rows=10,
                    )
                    self.assertFalse(result.accepted)
                    self.assertTrue(
                        {"duplicate_output_name", "output_alias_required"}
                        & {issue.code for issue in result.issues}
                    )

    def test_driver_names_are_checked_even_for_empty_results_before_fetching(self) -> None:
        cursor = Mock()
        with self.assertRaises(ReadOnlyExecutionError):
            _column_names((("id",), ("id",)))
        with self.assertRaises(ReadOnlyExecutionError):
            _fetch_bounded_rows(cursor, ("id", "id"), 10, 1024)
        cursor.fetchmany.assert_not_called()
        self.assertEqual(("id", "ID"), _column_names((("id",), ("ID",))))

    def test_distinct_aliases_keep_both_values_and_lineage(self) -> None:
        result = SQLValidatorRegistry().validate(
            SQLProposal(
                intent="data_query",
                dialect="postgresql",
                sql="SELECT id AS original_id, id + 101 AS shifted_id FROM public.orders",
                tables=("public.orders",),
                columns=("public.orders.id",),
            ),
            allowed_tables=frozenset({"public.orders"}),
            allowed_columns=frozenset({"public.orders.id"}),
            max_rows=10,
        )
        self.assertTrue(result.accepted, result.issues)
        self.assertEqual(
            ["original_id", "shifted_id"], [x.output_name for x in result.output_lineage]
        )
        cursor = Mock()
        cursor.fetchmany.return_value = [(101, 202)]
        rows, _, _ = _fetch_bounded_rows(cursor, ("original_id", "shifted_id"), 10, 1024)
        self.assertEqual(({"original_id": 101, "shifted_id": 202},), rows)
