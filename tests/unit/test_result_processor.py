from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from packages.domain.sqlverity_domain.contracts import ReadOnlyResult
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSource,
    DataSourceCapability,
    DataSourceType,
    LLMUsageEvent,
    OutputColumnLineage,
    QueryRequest,
    QueryRequestState,
)
from packages.result_engine.sqlverity_result_engine import (
    DeterministicResultProcessor,
    ResultShape,
)


class DeterministicResultProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = DeterministicResultProcessor()
        self.data_source = DataSource(
            id="source-1",
            tenant_id="tenant-1",
            name="Finance Reporting",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
            capabilities=frozenset({DataSourceCapability.EXECUTE_READ_ONLY}),
            connection_secret_ref="vault://finance",
        )
        approved_at = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
        self.query_request = QueryRequest(
            id="request-1",
            tenant_id="tenant-1",
            data_source_id="source-1",
            catalog_version_id="version-1",
            sql_text="SELECT total_amount FROM public.orders",
            normalized_sql="SELECT total_amount FROM public.orders LIMIT 500",
            referenced_tables=("public.orders",),
            referenced_columns=("public.orders.total_amount",),
            validation_issue_codes=("limit_added",),
            state=QueryRequestState.RESULT_PROCESSING,
            business_concepts=("revenue",),
            metrics=("gross_revenue",),
            business_rules=("valid_order",),
            assumptions=("Amounts use the source currency",),
            provider_id="provider-1",
            model_id="model-1",
            llm_usage_event_id="usage-1",
            estimated_db_cost=12.5,
            estimated_db_rows=1,
            explained_at=approved_at,
            approved_by="reviewer-1",
            approved_at=approved_at,
        )
        self.usage = LLMUsageEvent(
            id="usage-1",
            tenant_id="tenant-1",
            provider_id="provider-1",
            model_id="model-1",
            purpose="sql_proposal_generation",
            estimated_input_tokens=100,
            estimated_output_tokens=25,
            input_tokens=90,
            output_tokens=20,
            latency_ms=50,
            estimated_cost="0.004",
            actual_cost="0.0038",
        )

    def test_single_value_is_formatted_locally_with_full_provenance(self) -> None:
        processed = self.processor.process(
            query_request=self.query_request,
            data_source=self.data_source,
            result=self._result(columns=("total_amount",), rows=((14700000,),)),
            column_classifications={
                "public.orders.total_amount": Classification.INTERNAL
            },
            usage=self.usage,
        )

        self.assertEqual(ResultShape.SINGLE_VALUE, processed.answer.shape)
        self.assertEqual(
            "The query returned one value: 14700000.",
            processed.answer.summary,
        )
        self.assertEqual(14700000, processed.result.rows[0]["total_amount"])
        self.assertFalse(processed.privacy.raw_rows_sent_to_llm)
        self.assertFalse(processed.privacy.llm_interpretation_used)
        self.assertTrue(processed.privacy.output_lineage_complete)
        self.assertEqual("Finance Reporting", processed.provenance.data_source_name)
        self.assertEqual(("revenue",), processed.provenance.business_concepts)
        self.assertEqual(("gross_revenue",), processed.provenance.metrics)
        self.assertEqual(("valid_order",), processed.provenance.business_rules)
        self.assertEqual("0.0038", processed.provenance.actual_llm_cost)
        self.assertEqual(12.5, processed.provenance.estimated_db_cost)

    def test_sensitive_result_is_redacted_before_leaving_processor(self) -> None:
        processed = self.processor.process(
            query_request=self.query_request,
            data_source=self.data_source,
            result=self._result(columns=("total_amount",), rows=(("secret-value",),)),
            column_classifications={
                "public.orders.total_amount": Classification.CONFIDENTIAL
            },
            usage=self.usage,
        )

        self.assertEqual("[REDACTED]", processed.result.rows[0]["total_amount"])
        self.assertNotIn("secret-value", processed.answer.summary)
        self.assertEqual(
            Classification.CONFIDENTIAL,
            processed.privacy.maximum_classification,
        )
        self.assertEqual(("total_amount",), processed.privacy.masked_output_columns)
        self.assertIn(
            "values_redacted_by_local_display_policy",
            processed.privacy.warnings,
        )

    def test_empty_single_row_and_table_shapes_are_deterministic(self) -> None:
        cases = (
            (self._result(columns=("id",), rows=()), ResultShape.EMPTY),
            (self._result(columns=("id", "name"), rows=((1, "A"),)), ResultShape.SINGLE_ROW),
            (
                self._result(columns=("id",), rows=((1,), (2,)), truncated=True),
                ResultShape.TABLE,
            ),
        )
        request = self._request_for_column("public.orders.id")
        for result, expected_shape in cases:
            with self.subTest(shape=expected_shape):
                processed = self.processor.process(
                    query_request=request,
                    data_source=self.data_source,
                    result=result,
                    column_classifications={"public.orders.id": Classification.PUBLIC},
                    usage=None,
                )
                self.assertEqual(expected_shape, processed.answer.shape)
                if result.truncated:
                    self.assertIn("truncated by row_limit", processed.answer.summary)

    def test_alias_lineage_and_missing_classification_are_reported(self) -> None:
        processed = self.processor.process(
            query_request=self.query_request,
            data_source=self.data_source,
            result=self._result(columns=("revenue",), rows=((10,),)),
            column_classifications={},
            usage=None,
        )

        self.assertFalse(processed.privacy.output_lineage_complete)
        self.assertEqual(
            Classification.HIGHLY_SENSITIVE,
            processed.privacy.maximum_classification,
        )
        self.assertEqual("[REDACTED]", processed.result.rows[0]["revenue"])
        self.assertIn(
            "classification_missing:public.orders.total_amount",
            processed.privacy.warnings,
        )
        self.assertIn(
            "output_column_lineage_is_conservative",
            processed.privacy.warnings,
        )

    def test_complete_lineage_masks_only_sensitive_output_columns(self) -> None:
        request = replace(
            self.query_request,
            referenced_columns=(
                "public.orders.id",
                "public.orders.total_amount",
            ),
            output_lineage=(
                OutputColumnLineage(
                    output_name="id",
                    source_columns=("public.orders.id",),
                ),
                OutputColumnLineage(
                    output_name="revenue",
                    source_columns=("public.orders.total_amount",),
                ),
            ),
            output_lineage_complete=True,
        )
        processed = self.processor.process(
            query_request=request,
            data_source=self.data_source,
            result=self._result(
                columns=("id", "revenue"),
                rows=((7, "sensitive"),),
            ),
            column_classifications={
                "public.orders.id": Classification.PUBLIC,
                "public.orders.total_amount": Classification.CONFIDENTIAL,
            },
            usage=None,
        )

        self.assertEqual(7, processed.result.rows[0]["id"])
        self.assertEqual("[REDACTED]", processed.result.rows[0]["revenue"])
        self.assertEqual(("revenue",), processed.privacy.masked_output_columns)
        self.assertTrue(processed.privacy.output_lineage_complete)

    def test_large_scalar_is_truncated_only_in_summary(self) -> None:
        processor = DeterministicResultProcessor(maximum_summary_characters=20)
        value = "x" * 100
        processed = processor.process(
            query_request=self.query_request,
            data_source=self.data_source,
            result=self._result(columns=("total_amount",), rows=((value,),)),
            column_classifications={
                "public.orders.total_amount": Classification.INTERNAL
            },
            usage=None,
        )

        self.assertEqual(value, processed.result.rows[0]["total_amount"])
        self.assertIn("…", processed.answer.summary)
        self.assertIn("single_value_summary_truncated", processed.privacy.warnings)

    @staticmethod
    def _result(
        *,
        columns: tuple[str, ...],
        rows: tuple[tuple[object, ...], ...],
        truncated: bool = False,
    ) -> ReadOnlyResult:
        mapped_rows = tuple(
            dict(zip(columns, row, strict=True))
            for row in rows
        )
        return ReadOnlyResult(
            columns=columns,
            rows=mapped_rows,
            row_count=len(rows),
            truncated=truncated,
            truncation_reason="row_limit" if truncated else None,
            result_bytes=100,
            elapsed_ms=5,
        )

    def _request_for_column(self, column_ref: str) -> QueryRequest:
        return QueryRequest(
            id="request-shape",
            tenant_id="tenant-1",
            data_source_id="source-1",
            catalog_version_id="version-1",
            sql_text="SELECT id FROM public.orders",
            normalized_sql="SELECT id FROM public.orders LIMIT 500",
            referenced_tables=("public.orders",),
            referenced_columns=(column_ref,),
            validation_issue_codes=(),
            state=QueryRequestState.RESULT_PROCESSING,
        )


if __name__ == "__main__":
    unittest.main()
