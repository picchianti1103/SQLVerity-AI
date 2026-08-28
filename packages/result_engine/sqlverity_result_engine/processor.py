from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any

from packages.domain.sqlverity_domain.contracts import ReadOnlyResult
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSource,
    LLMUsageEvent,
    QueryRequest,
    utc_now,
)


class ResultShape(StrEnum):
    EMPTY = "empty"
    SINGLE_VALUE = "single_value"
    SINGLE_ROW = "single_row"
    TABLE = "table"


@dataclass(frozen=True, slots=True)
class ClassificationCount:
    classification: Classification
    column_count: int


@dataclass(frozen=True, slots=True)
class DeterministicAnswer:
    shape: ResultShape
    summary: str
    deterministic: bool = True


@dataclass(frozen=True, slots=True)
class ResultPrivacyReport:
    processing_mode: str
    maximum_classification: Classification
    classification_counts: tuple[ClassificationCount, ...]
    raw_rows_sent_to_llm: bool
    llm_interpretation_used: bool
    masked_output_columns: tuple[str, ...]
    output_lineage_complete: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultProvenance:
    request_id: str
    data_source_id: str
    data_source_name: str
    catalog_version_id: str
    sql: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    metrics: tuple[str, ...]
    business_rules: tuple[str, ...]
    assumptions: tuple[str, ...]
    approved_by: str | None
    approved_at: datetime | None
    executed_at: datetime
    provider_id: str | None
    model_id: str | None
    llm_usage_event_id: str | None
    estimated_llm_cost: str | None
    actual_llm_cost: str | None
    estimated_db_cost: float | None
    estimated_db_rows: int | None
    row_count: int
    truncated: bool
    truncation_reason: str | None
    result_bytes: int
    execution_elapsed_ms: int
    parameter_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProcessedQueryResult:
    result: ReadOnlyResult
    answer: DeterministicAnswer
    privacy: ResultPrivacyReport
    provenance: ResultProvenance


class DeterministicResultProcessor:
    """Local result renderer that never invokes or prepares payloads for an LLM."""

    def __init__(
        self,
        *,
        maximum_visible_classification: Classification = Classification.INTERNAL,
        maximum_summary_characters: int = 200,
    ) -> None:
        if not 20 <= maximum_summary_characters <= 2_000:
            raise ValueError("maximum_summary_characters must be between 20 and 2000")
        self._maximum_visible_classification = maximum_visible_classification
        self._maximum_summary_characters = maximum_summary_characters

    def process(
        self,
        *,
        query_request: QueryRequest,
        data_source: DataSource,
        result: ReadOnlyResult,
        column_classifications: Mapping[str, Classification],
        usage: LLMUsageEvent | None,
    ) -> ProcessedQueryResult:
        warnings: list[str] = []
        classifications: list[Classification] = []
        for column_ref in query_request.referenced_columns:
            classification = column_classifications.get(column_ref)
            if classification is None:
                classification = Classification.HIGHLY_SENSITIVE
                warnings.append(f"classification_missing:{column_ref}")
            classifications.append(classification)
        maximum_classification = _maximum_classification(classifications)
        counts = Counter(classifications)
        classification_counts = tuple(
            ClassificationCount(classification, counts[classification])
            for classification in Classification
            if counts[classification]
        )
        output_classifications, output_lineage_complete = _output_classifications(
            query_request,
            result.columns,
            column_classifications,
        )
        if not output_lineage_complete and result.columns:
            warnings.append("output_column_lineage_is_conservative")

        should_mask = _above_visible(
            maximum_classification,
            self._maximum_visible_classification,
        )
        if output_lineage_complete:
            masked_columns = tuple(
                column
                for column in result.columns
                if _above_visible(
                    output_classifications[column.casefold()],
                    self._maximum_visible_classification,
                )
            )
        else:
            masked_columns = result.columns if should_mask else ()
        masked_column_keys = {column.casefold() for column in masked_columns}
        safe_rows: tuple[Mapping[str, Any], ...]
        if masked_columns:
            safe_rows = tuple(
                {
                    column: (
                        None
                        if value is None
                        else "[REDACTED]"
                        if column.casefold() in masked_column_keys
                        else value
                    )
                    for column, value in row.items()
                }
                for row in result.rows
            )
            warnings.append("values_redacted_by_local_display_policy")
        else:
            safe_rows = result.rows

        safe_result = ReadOnlyResult(
            columns=result.columns,
            rows=safe_rows,
            row_count=result.row_count,
            truncated=result.truncated,
            truncation_reason=result.truncation_reason,
            result_bytes=result.result_bytes,
            elapsed_ms=result.elapsed_ms,
        )
        answer, answer_warnings = self._answer(
            safe_result,
            maximum_classification,
            bool(masked_columns),
        )
        warnings.extend(answer_warnings)
        privacy = ResultPrivacyReport(
            processing_mode="deterministic_local",
            maximum_classification=maximum_classification,
            classification_counts=classification_counts,
            raw_rows_sent_to_llm=False,
            llm_interpretation_used=False,
            masked_output_columns=masked_columns,
            output_lineage_complete=output_lineage_complete,
            warnings=tuple(dict.fromkeys(warnings)),
        )
        provenance = ResultProvenance(
            request_id=query_request.id,
            data_source_id=data_source.id,
            data_source_name=data_source.name,
            catalog_version_id=query_request.catalog_version_id,
            sql=query_request.normalized_sql or query_request.sql_text,
            tables=query_request.referenced_tables,
            columns=query_request.referenced_columns,
            business_concepts=query_request.business_concepts,
            metrics=query_request.metrics,
            business_rules=query_request.business_rules,
            assumptions=query_request.assumptions,
            approved_by=query_request.approved_by,
            approved_at=query_request.approved_at,
            executed_at=utc_now(),
            provider_id=query_request.provider_id,
            model_id=query_request.model_id,
            llm_usage_event_id=query_request.llm_usage_event_id,
            estimated_llm_cost=usage.estimated_cost if usage is not None else None,
            actual_llm_cost=usage.actual_cost if usage is not None else None,
            estimated_db_cost=query_request.estimated_db_cost,
            estimated_db_rows=query_request.estimated_db_rows,
            row_count=result.row_count,
            truncated=result.truncated,
            truncation_reason=result.truncation_reason,
            result_bytes=result.result_bytes,
            execution_elapsed_ms=result.elapsed_ms,
            parameter_names=query_request.parameter_names,
        )
        return ProcessedQueryResult(
            result=safe_result,
            answer=answer,
            privacy=privacy,
            provenance=provenance,
        )

    def _answer(
        self,
        result: ReadOnlyResult,
        maximum_classification: Classification,
        masked: bool,
    ) -> tuple[DeterministicAnswer, tuple[str, ...]]:
        warnings: list[str] = []
        shape = _result_shape(result)
        if shape is ResultShape.EMPTY:
            summary = "The query returned no rows."
        elif masked:
            summary = (
                f"The query returned {result.row_count} row(s); values are redacted "
                f"at classification {maximum_classification.value}."
            )
        elif shape is ResultShape.SINGLE_VALUE:
            value = next(iter(result.rows[0].values()))
            rendered, was_truncated = _format_value(
                value,
                self._maximum_summary_characters,
            )
            summary = f"The query returned one value: {rendered}."
            if was_truncated:
                warnings.append("single_value_summary_truncated")
        elif shape is ResultShape.SINGLE_ROW:
            summary = f"The query returned one row with {len(result.columns)} columns."
        else:
            summary = (
                f"The query returned {result.row_count} rows "
                f"across {len(result.columns)} columns."
            )
        if result.truncated:
            summary += f" The result was truncated by {result.truncation_reason}."
        return DeterministicAnswer(shape=shape, summary=summary), tuple(warnings)


def _result_shape(result: ReadOnlyResult) -> ResultShape:
    if result.row_count == 0:
        return ResultShape.EMPTY
    if result.row_count == 1 and len(result.columns) == 1:
        return ResultShape.SINGLE_VALUE
    if result.row_count == 1:
        return ResultShape.SINGLE_ROW
    return ResultShape.TABLE


def _maximum_classification(values: list[Classification]) -> Classification:
    if not values:
        return Classification.INTERNAL
    return max(values, key=_CLASSIFICATION_RANK.__getitem__)


def _output_lineage_complete(
    output_columns: tuple[str, ...],
    referenced_columns: tuple[str, ...],
) -> bool:
    physical_names = Counter(
        column_ref.rsplit(".", 1)[-1].casefold()
        for column_ref in referenced_columns
    )
    return all(physical_names[column.casefold()] == 1 for column in output_columns)


def _output_classifications(
    query_request: QueryRequest,
    output_columns: tuple[str, ...],
    column_classifications: Mapping[str, Classification],
) -> tuple[dict[str, Classification], bool]:
    if query_request.output_lineage:
        lineage = {
            item.output_name.casefold(): item
            for item in query_request.output_lineage
        }
        output_names = tuple(column.casefold() for column in output_columns)
        complete = (
            query_request.output_lineage_complete
            and len(output_names) == len(set(output_names)) == len(lineage)
            and set(lineage) == set(output_names)
        )
        if not complete:
            return {}, False
        return (
            {
                column.casefold(): _maximum_classification(
                    [
                        column_classifications.get(
                            source_column,
                            Classification.HIGHLY_SENSITIVE,
                        )
                        for source_column in lineage[column.casefold()].source_columns
                    ]
                )
                for column in output_columns
            },
            True,
        )

    complete = _output_lineage_complete(
        output_columns,
        query_request.referenced_columns,
    )
    if not complete:
        return {}, False
    physical_by_name = {
        column_ref.rsplit(".", 1)[-1].casefold(): column_ref
        for column_ref in query_request.referenced_columns
    }
    return (
        {
            column.casefold(): column_classifications.get(
                physical_by_name[column.casefold()],
                Classification.HIGHLY_SENSITIVE,
            )
            for column in output_columns
        },
        True,
    )


def _above_visible(
    classification: Classification,
    maximum_visible: Classification,
) -> bool:
    return (
        _CLASSIFICATION_RANK[classification]
        > _CLASSIFICATION_RANK[maximum_visible]
    )


def _format_value(value: Any, maximum_characters: int) -> tuple[str, bool]:
    if value is None:
        rendered = "NULL"
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, Decimal):
        rendered = format(value, "f")
    elif isinstance(value, float):
        rendered = str(value) if math.isfinite(value) else "non-finite number"
    elif isinstance(value, (datetime, date, time)):
        rendered = value.isoformat()
    elif isinstance(value, bytes):
        rendered = f"<{len(value)} bytes>"
    elif isinstance(value, (Mapping, list, tuple)):
        rendered = json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = str(value)
    if len(rendered) <= maximum_characters:
        return rendered, False
    return rendered[: maximum_characters - 1] + "…", True


_CLASSIFICATION_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.PII: 3,
    Classification.HIGHLY_SENSITIVE: 4,
}
