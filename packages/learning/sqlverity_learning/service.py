from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from packages.catalog.sqlverity_catalog.explorer import CatalogNotIngestedError
from packages.catalog.sqlverity_catalog.ingestion import DataSourceNotFoundError
from packages.catalog.sqlverity_catalog.repository import (
    CorrectedSQLExampleConflictError,
    SQLiteCatalogRepository,
)
from packages.domain.sqlverity_domain.contracts import (
    SQLProposal,
    SQLValidator,
    ValidationIssue,
    ValidationResult,
)
from packages.domain.sqlverity_domain.models import (
    CatalogVersion,
    Classification,
    CorrectedSQLExample,
    DataSource,
)


class CorrectedSQLValidationError(ValueError):
    def __init__(self, validation: ValidationResult) -> None:
        self.validation = validation
        codes = tuple(issue.code for issue in validation.issues if issue.blocking)
        super().__init__(f"Corrected SQL failed validation: {codes}")


class CorrectedSQLConcurrencyError(RuntimeError):
    pass


class CorrectedSQLExampleNotFoundError(LookupError):
    pass


class CorrectedSQLSourceNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class CorrectedSQLExampleEntry:
    example: CorrectedSQLExample
    is_active: bool


@dataclass(frozen=True, slots=True)
class SQLExampleMatch:
    example: CorrectedSQLExample
    score: float


class LearningLoopService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        validator: SQLValidator,
        *,
        max_preview_rows: int = 500,
        minimum_retrieval_score: float = 0.2,
    ) -> None:
        if not 1 <= max_preview_rows <= 10_000:
            raise ValueError("max_preview_rows must be between 1 and 10000")
        if not 0 < minimum_retrieval_score <= 1:
            raise ValueError("minimum_retrieval_score must be between zero and one")
        self._repository = repository
        self._validator = validator
        self._max_preview_rows = max_preview_rows
        self._minimum_retrieval_score = minimum_retrieval_score

    def correct(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        question: str,
        corrected_sql: str,
        actor_id: str,
        content_classification: Classification,
        business_concepts: tuple[str, ...] = (),
        assumptions: tuple[str, ...] = (),
        reason: str | None = None,
        source_query_request_id: str | None = None,
        supersedes_example_id: str | None = None,
    ) -> CorrectedSQLExampleEntry:
        data_source, version, allowed_tables, allowed_columns = self._current_context(
            tenant_id,
            data_source_id,
        )
        question = question.strip()
        normalized_question = _normalize_question(question)
        if not normalized_question:
            raise ValueError("Corrected SQL question has no searchable terms")
        if source_query_request_id is not None:
            source_request = self._repository.get_query_request(
                tenant_id,
                source_query_request_id,
            )
            if (
                source_request is None
                or source_request.data_source_id != data_source_id
            ):
                raise CorrectedSQLSourceNotFoundError(
                    "Source query request does not exist in this DataSource"
                )

        entries = self.list_examples(
            tenant_id,
            data_source_id,
            include_superseded=True,
        )
        active = {entry.example.id: entry.example for entry in entries if entry.is_active}
        active_for_question = tuple(
            example
            for example in active.values()
            if example.normalized_question == normalized_question
        )
        if supersedes_example_id is None:
            if active_for_question:
                raise CorrectedSQLConcurrencyError(
                    "An active corrected SQL example already exists for this question"
                )
            revision = 1
        else:
            predecessor = active.get(supersedes_example_id)
            if predecessor is None:
                raise CorrectedSQLConcurrencyError(
                    "Corrected SQL predecessor is missing or already superseded"
                )
            if predecessor.normalized_question != normalized_question:
                raise CorrectedSQLConcurrencyError(
                    "Corrected SQL predecessor belongs to another question"
                )
            revision = predecessor.revision + 1

        validation = self._validate_sql(
            corrected_sql.strip(),
            data_source.dialect,
            allowed_tables,
            allowed_columns,
            business_concepts,
            assumptions,
        )
        normalized_sql = validation.normalized_sql
        if normalized_sql is None:
            raise CorrectedSQLValidationError(validation)
        example = CorrectedSQLExample(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            question=question,
            normalized_question=normalized_question,
            content_classification=content_classification,
            sql_text=corrected_sql.strip(),
            normalized_sql=normalized_sql,
            referenced_tables=validation.referenced_tables,
            referenced_columns=validation.referenced_columns,
            business_concepts=tuple(item.strip() for item in business_concepts),
            assumptions=tuple(item.strip() for item in assumptions),
            actor_id=actor_id.strip(),
            reason=reason.strip() if reason is not None else None,
            source_query_request_id=source_query_request_id,
            supersedes_example_id=supersedes_example_id,
            revision=revision,
        )
        try:
            stored = self._repository.create_corrected_sql_example(example)
        except CorrectedSQLExampleConflictError as error:
            raise CorrectedSQLConcurrencyError(str(error)) from error
        return CorrectedSQLExampleEntry(example=stored, is_active=True)

    def list_examples(
        self,
        tenant_id: str,
        data_source_id: str,
        *,
        include_superseded: bool = False,
    ) -> tuple[CorrectedSQLExampleEntry, ...]:
        if self._repository.get_data_source(tenant_id, data_source_id) is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        examples = self._repository.list_corrected_sql_examples(
            tenant_id,
            data_source_id,
        )
        superseded_ids = frozenset(
            example.supersedes_example_id
            for example in examples
            if example.supersedes_example_id is not None
        )
        entries = tuple(
            CorrectedSQLExampleEntry(
                example=example,
                is_active=example.id not in superseded_ids,
            )
            for example in examples
        )
        if include_superseded:
            return entries
        return tuple(entry for entry in entries if entry.is_active)

    def retrieve(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        question: str,
        max_results: int = 3,
    ) -> tuple[SQLExampleMatch, ...]:
        if not 0 <= max_results <= 10:
            raise ValueError("max_results must be between zero and ten")
        _, _, allowed_tables, allowed_columns = self._current_context(
            tenant_id,
            data_source_id,
        )
        if max_results == 0:
            return ()
        normalized_question = _normalize_question(question)
        query_terms = frozenset(normalized_question.split())
        if not query_terms:
            raise ValueError("Retrieval question has no searchable terms")
        matches: list[SQLExampleMatch] = []
        for entry in self.list_examples(tenant_id, data_source_id):
            example = entry.example
            if not set(example.referenced_tables).issubset(allowed_tables):
                continue
            if not set(example.referenced_columns).issubset(allowed_columns):
                continue
            example_terms = frozenset(example.normalized_question.split())
            score = (
                1.0
                if example.normalized_question == normalized_question
                else _jaccard(query_terms, example_terms)
            )
            if score >= self._minimum_retrieval_score:
                matches.append(SQLExampleMatch(example=example, score=score))
        matches.sort(
            key=lambda match: (
                -match.score,
                match.example.normalized_question,
                match.example.id,
            )
        )
        return tuple(matches[:max_results])

    def _current_context(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[DataSource, CatalogVersion, frozenset[str], frozenset[str]]:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        version = self._repository.get_latest_catalog_version(tenant_id, data_source_id)
        if version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")
        schema_objects = self._repository.list_schema_objects(tenant_id, version.id)
        objects_by_id = {schema_object.id: schema_object for schema_object in schema_objects}
        allowed_tables = frozenset(item.reference for item in schema_objects)
        allowed_columns = frozenset(
            f"{objects_by_id[column.schema_object_id].reference}.{column.name}"
            for column in self._repository.list_columns_for_catalog_version(
                tenant_id,
                version.id,
            )
        )
        return data_source, version, allowed_tables, allowed_columns

    def _validate_sql(
        self,
        sql: str,
        dialect: str,
        allowed_tables: frozenset[str],
        allowed_columns: frozenset[str],
        business_concepts: tuple[str, ...],
        assumptions: tuple[str, ...],
    ) -> ValidationResult:
        probe = self._validator.validate(
            SQLProposal(intent="data_query", sql=sql, dialect=dialect),
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            max_rows=self._max_preview_rows,
        )
        validation = self._validator.validate(
            SQLProposal(
                intent="data_query",
                sql=sql,
                dialect=dialect,
                tables=probe.referenced_tables,
                columns=probe.referenced_columns,
                business_concepts=business_concepts,
                assumptions=assumptions,
            ),
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            max_rows=self._max_preview_rows,
        )
        if not validation.accepted or validation.normalized_sql is None:
            raise CorrectedSQLValidationError(validation)
        if not validation.referenced_tables:
            raise CorrectedSQLValidationError(
                ValidationResult(
                    dialect=validation.dialect,
                    normalized_sql=None,
                    issues=(
                        ValidationIssue(
                            code="corrected_sql_no_table",
                            message="Corrected SQL must reference at least one catalog table",
                        ),
                    ),
                    referenced_tables=validation.referenced_tables,
                    referenced_columns=validation.referenced_columns,
                )
            )
        return validation


def _normalize_question(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", without_marks)
    tokens = re.findall(r"\w+", camel_split.casefold(), flags=re.UNICODE)
    return " ".join(token for token in tokens if token not in _STOP_WORDS)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


_STOP_WORDS = frozenset(
    {
        "a",
        "and",
        "con",
        "da",
        "dei",
        "del",
        "della",
        "dimmi",
        "di",
        "e",
        "for",
        "gli",
        "i",
        "il",
        "in",
        "la",
        "le",
        "mostra",
        "of",
        "per",
        "qual",
        "quale",
        "quali",
        "quanta",
        "quante",
        "quanti",
        "quanto",
        "show",
        "the",
        "un",
        "una",
    }
)
