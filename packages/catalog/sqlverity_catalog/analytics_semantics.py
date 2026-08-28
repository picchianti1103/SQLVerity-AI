from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from packages.domain.sqlverity_domain.models import (
    AnalyticSemanticKind,
    BusinessRuleDefinition,
    BusinessRuleResolution,
    CatalogVersion,
    Classification,
    EpistemicStatus,
    MetricDefinition,
    MetricResolution,
)
from packages.domain.sqlverity_domain.text import normalize_search_term as _normalize_term
from packages.sql_engine.sqlverity_sql_engine import (
    UnsupportedDialectError,
    sqlglot_dialect_name,
)

from .business_concepts import BusinessConceptService
from .explorer import CatalogNotIngestedError
from .ingestion import DataSourceNotFoundError
from .repository import (
    AnalyticSemanticDefinition,
    AnalyticSemanticResolution,
    AnalyticSemanticResolutionConflictError,
    AnalyticSemanticWriteResult,
    SQLiteCatalogRepository,
)


class AnalyticSemanticNotFoundError(LookupError):
    pass


class AnalyticSemanticReferenceError(LookupError):
    pass


class AnalyticSemanticValidationError(ValueError):
    pass


class AnalyticSemanticConcurrencyError(RuntimeError):
    pass


class AnalyticSemanticNameConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnalyticSemanticEvidenceEntry:
    definition: AnalyticSemanticDefinition
    selected: bool


@dataclass(frozen=True, slots=True)
class AnalyticSemanticReviewItem:
    resolution: AnalyticSemanticResolution
    evidence: tuple[AnalyticSemanticEvidenceEntry, ...]


@dataclass(frozen=True, slots=True)
class MetricMatch:
    resolution: MetricResolution
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessRuleMatch:
    resolution: BusinessRuleResolution
    matched_terms: tuple[str, ...]
    selected_by_metrics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalyticSemanticContext:
    metrics: tuple[MetricMatch, ...]
    business_rules: tuple[BusinessRuleMatch, ...]


class AnalyticsSemanticsService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        business_concepts: BusinessConceptService,
    ) -> None:
        self._repository = repository
        self._business_concepts = business_concepts

    def propose_metric(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        metric_key: str,
        name: str,
        description: str,
        expression_sql: str,
        grain_refs: tuple[str, ...],
        dimension_refs: tuple[str, ...],
        concept_keys: tuple[str, ...],
        rule_keys: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None = None,
        reason: str | None = None,
    ) -> AnalyticSemanticWriteResult:
        self._require_proposal_status(status)
        definition = self._metric_definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            metric_key=metric_key,
            name=name,
            description=description,
            expression_sql=expression_sql,
            grain_refs=grain_refs,
            dimension_refs=dimension_refs,
            concept_keys=concept_keys,
            rule_keys=rule_keys,
            content_classification=content_classification,
            status=status,
            source=source,
            confidence=confidence,
            actor_id=actor_id,
            reason=reason,
        )
        return self._repository.propose_analytic_semantic_definition(definition)

    def correct_metric(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        metric_key: str,
        name: str,
        description: str,
        expression_sql: str,
        grain_refs: tuple[str, ...],
        dimension_refs: tuple[str, ...],
        concept_keys: tuple[str, ...],
        rule_keys: tuple[str, ...],
        content_classification: Classification,
        actor_id: str,
        reason: str | None = None,
        expected_updated_at: datetime | None = None,
    ) -> AnalyticSemanticWriteResult:
        definition = self._metric_definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            metric_key=metric_key,
            name=name,
            description=description,
            expression_sql=expression_sql,
            grain_refs=grain_refs,
            dimension_refs=dimension_refs,
            concept_keys=concept_keys,
            rule_keys=rule_keys,
            content_classification=content_classification,
            status=EpistemicStatus.CONFIRMED,
            source="human_correction",
            confidence=1.0,
            actor_id=actor_id,
            reason=reason,
        )
        self._require_unique_confirmed_name(definition)
        return self._correct(definition, expected_updated_at)

    def propose_business_rule(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        rule_key: str,
        name: str,
        description: str,
        predicate_sql: str,
        concept_keys: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None = None,
        reason: str | None = None,
    ) -> AnalyticSemanticWriteResult:
        self._require_proposal_status(status)
        definition = self._rule_definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            rule_key=rule_key,
            name=name,
            description=description,
            predicate_sql=predicate_sql,
            concept_keys=concept_keys,
            content_classification=content_classification,
            status=status,
            source=source,
            confidence=confidence,
            actor_id=actor_id,
            reason=reason,
        )
        return self._repository.propose_analytic_semantic_definition(definition)

    def correct_business_rule(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        rule_key: str,
        name: str,
        description: str,
        predicate_sql: str,
        concept_keys: tuple[str, ...],
        content_classification: Classification,
        actor_id: str,
        reason: str | None = None,
        expected_updated_at: datetime | None = None,
    ) -> AnalyticSemanticWriteResult:
        definition = self._rule_definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            rule_key=rule_key,
            name=name,
            description=description,
            predicate_sql=predicate_sql,
            concept_keys=concept_keys,
            content_classification=content_classification,
            status=EpistemicStatus.CONFIRMED,
            source="human_correction",
            confidence=1.0,
            actor_id=actor_id,
            reason=reason,
        )
        self._require_unique_confirmed_name(definition)
        return self._correct(definition, expected_updated_at)

    def list_metrics(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[MetricResolution, ...]:
        self._require_data_source(tenant_id, data_source_id)
        return tuple(
            item
            for item in self._repository.list_analytic_semantic_resolutions(
                tenant_id,
                data_source_id,
                kind=AnalyticSemanticKind.METRIC,
            )
            if isinstance(item, MetricResolution)
        )

    def list_business_rules(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[BusinessRuleResolution, ...]:
        self._require_data_source(tenant_id, data_source_id)
        return tuple(
            item
            for item in self._repository.list_analytic_semantic_resolutions(
                tenant_id,
                data_source_id,
                kind=AnalyticSemanticKind.BUSINESS_RULE,
            )
            if isinstance(item, BusinessRuleResolution)
        )

    def history(
        self,
        tenant_id: str,
        data_source_id: str,
        kind: AnalyticSemanticKind,
        asset_key: str,
    ) -> tuple[AnalyticSemanticEvidenceEntry, ...]:
        self._require_data_source(tenant_id, data_source_id)
        current = self._repository.get_analytic_semantic_resolution(
            tenant_id,
            data_source_id,
            kind,
            asset_key,
        )
        if current is None:
            raise AnalyticSemanticNotFoundError("Analytic semantic asset does not exist")
        return tuple(
            AnalyticSemanticEvidenceEntry(
                definition=definition,
                selected=current.selected_definition_id == definition.id,
            )
            for definition in self._repository.list_analytic_semantic_definitions(
                tenant_id,
                data_source_id,
                kind,
                asset_key,
            )
        )

    def list_review_queue(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[AnalyticSemanticReviewItem, ...]:
        self._require_data_source(tenant_id, data_source_id)
        resolutions = self._repository.list_analytic_semantic_resolutions(
            tenant_id,
            data_source_id,
            statuses=frozenset({EpistemicStatus.INFERRED, EpistemicStatus.CONFLICTING}),
        )
        return tuple(
            AnalyticSemanticReviewItem(
                resolution=resolution,
                evidence=self.history(
                    tenant_id,
                    data_source_id,
                    _kind(resolution),
                    _key(resolution),
                ),
            )
            for resolution in resolutions
        )

    def resolve_for_query(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        query: str,
        concept_keys: frozenset[str] = frozenset(),
    ) -> AnalyticSemanticContext:
        self._require_data_source(tenant_id, data_source_id)
        normalized_query = f" {_normalize_term(query)} "
        metrics: list[MetricMatch] = []
        for metric_resolution in self.list_metrics(tenant_id, data_source_id):
            if metric_resolution.status is not EpistemicStatus.CONFIRMED:
                continue
            matched_terms = _matched_terms(
                normalized_query,
                metric_resolution.name,
                metric_resolution.metric_key,
            )
            if matched_terms or concept_keys.intersection(metric_resolution.concept_keys):
                metrics.append(MetricMatch(metric_resolution, matched_terms))
        selected_by_rule: dict[str, list[str]] = {}
        for metric_match in metrics:
            for rule_key in metric_match.resolution.rule_keys:
                selected_by_rule.setdefault(rule_key, []).append(
                    metric_match.resolution.metric_key
                )
        rules: list[BusinessRuleMatch] = []
        for rule in self.list_business_rules(tenant_id, data_source_id):
            if rule.status is not EpistemicStatus.CONFIRMED:
                continue
            matched_terms = _matched_terms(normalized_query, rule.name, rule.rule_key)
            selected_by = tuple(sorted(selected_by_rule.get(rule.rule_key, ())))
            if matched_terms or selected_by or concept_keys.intersection(rule.concept_keys):
                rules.append(BusinessRuleMatch(rule, matched_terms, selected_by))
        return AnalyticSemanticContext(tuple(metrics), tuple(rules))

    def _metric_definition(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        metric_key: str,
        name: str,
        description: str,
        expression_sql: str,
        grain_refs: tuple[str, ...],
        dimension_refs: tuple[str, ...],
        concept_keys: tuple[str, ...],
        rule_keys: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None,
        reason: str | None,
    ) -> MetricDefinition:
        version, known_refs, dialect = self._latest_context(tenant_id, data_source_id)
        normalized, expression_refs = _validate_metric_expression(expression_sql, dialect)
        grain = _canonical_values(grain_refs)
        dimensions = _canonical_values(dimension_refs)
        if not grain:
            raise ValueError("Metric grain requires at least one column reference")
        self._require_known_refs(grain, known_refs)
        self._require_known_refs(dimensions, known_refs)
        object_refs = tuple(
            sorted(set(expression_refs) | set(grain) | set(dimensions))
        )
        self._require_known_refs(object_refs, known_refs)
        concepts = _canonical_keys(concept_keys)
        rules = _canonical_keys(rule_keys)
        self._require_confirmed_concepts(tenant_id, data_source_id, concepts)
        self._require_confirmed_rules(tenant_id, data_source_id, rules)
        return MetricDefinition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            metric_key=_clean_key(metric_key),
            name=name.strip(),
            description=description.strip(),
            expression_sql=expression_sql.strip(),
            normalized_expression_sql=normalized,
            object_refs=object_refs,
            grain_refs=grain,
            dimension_refs=dimensions,
            concept_keys=concepts,
            rule_keys=rules,
            content_classification=content_classification,
            status=status,
            source=source.strip(),
            confidence=confidence,
            actor_id=actor_id.strip() if actor_id is not None else None,
            reason=reason.strip() if reason is not None else None,
        )

    def _rule_definition(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        rule_key: str,
        name: str,
        description: str,
        predicate_sql: str,
        concept_keys: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None,
        reason: str | None,
    ) -> BusinessRuleDefinition:
        version, known_refs, dialect = self._latest_context(tenant_id, data_source_id)
        normalized, object_refs = _validate_rule_predicate(predicate_sql, dialect)
        self._require_known_refs(object_refs, known_refs)
        concepts = _canonical_keys(concept_keys)
        self._require_confirmed_concepts(tenant_id, data_source_id, concepts)
        return BusinessRuleDefinition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            rule_key=_clean_key(rule_key),
            name=name.strip(),
            description=description.strip(),
            predicate_sql=predicate_sql.strip(),
            normalized_predicate_sql=normalized,
            object_refs=object_refs,
            concept_keys=concepts,
            content_classification=content_classification,
            status=status,
            source=source.strip(),
            confidence=confidence,
            actor_id=actor_id.strip() if actor_id is not None else None,
            reason=reason.strip() if reason is not None else None,
        )

    def _correct(
        self,
        definition: AnalyticSemanticDefinition,
        expected_updated_at: datetime | None,
    ) -> AnalyticSemanticWriteResult:
        try:
            return self._repository.propose_analytic_semantic_definition(
                definition,
                explicit_supersede=True,
                expected_updated_at=expected_updated_at,
            )
        except AnalyticSemanticResolutionConflictError as error:
            raise AnalyticSemanticConcurrencyError(str(error)) from error

    def _latest_context(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[CatalogVersion, frozenset[str], str]:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        version = self._repository.get_latest_catalog_version(tenant_id, data_source_id)
        if version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")
        schema_objects = self._repository.list_schema_objects(tenant_id, version.id)
        objects_by_id = {schema_object.id: schema_object for schema_object in schema_objects}
        references = frozenset(
            f"{objects_by_id[column.schema_object_id].reference}.{column.name}"
            for column in self._repository.list_columns_for_catalog_version(
                tenant_id,
                version.id,
            )
        )
        return version, references, data_source.dialect

    def _require_data_source(self, tenant_id: str, data_source_id: str) -> None:
        if self._repository.get_data_source(tenant_id, data_source_id) is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")

    @staticmethod
    def _require_known_refs(values: tuple[str, ...], known_refs: frozenset[str]) -> None:
        missing = tuple(value for value in values if value not in known_refs)
        if missing:
            raise AnalyticSemanticReferenceError(
                f"Unknown analytic semantic columns: {', '.join(missing)}"
            )

    def _require_confirmed_concepts(
        self,
        tenant_id: str,
        data_source_id: str,
        concept_keys: tuple[str, ...],
    ) -> None:
        resolutions = {
            item.concept_key: item
            for item in self._business_concepts.list_concepts(tenant_id, data_source_id)
        }
        missing = tuple(
            key
            for key in concept_keys
            if key not in resolutions
            or resolutions[key].status is not EpistemicStatus.CONFIRMED
        )
        if missing:
            raise AnalyticSemanticReferenceError(
                f"Concept dependencies are not confirmed: {', '.join(missing)}"
            )

    def _require_confirmed_rules(
        self,
        tenant_id: str,
        data_source_id: str,
        rule_keys: tuple[str, ...],
    ) -> None:
        resolutions = {
            item.rule_key: item
            for item in self.list_business_rules(tenant_id, data_source_id)
        }
        missing = tuple(
            key
            for key in rule_keys
            if key not in resolutions
            or resolutions[key].status is not EpistemicStatus.CONFIRMED
        )
        if missing:
            raise AnalyticSemanticReferenceError(
                f"Business-rule dependencies are not confirmed: {', '.join(missing)}"
            )

    def _require_unique_confirmed_name(
        self,
        candidate: AnalyticSemanticDefinition,
    ) -> None:
        kind = _kind(candidate)
        candidate_key = _key(candidate)
        candidate_name = _normalize_term(candidate.name)
        for current in self._repository.list_analytic_semantic_resolutions(
            candidate.tenant_id,
            candidate.data_source_id,
            kind=kind,
            statuses=frozenset({EpistemicStatus.CONFIRMED}),
        ):
            if _key(current) != candidate_key and _normalize_term(current.name) == candidate_name:
                raise AnalyticSemanticNameConflictError(
                    f"Confirmed {kind.value} name already belongs to {_key(current)}"
                )

    @staticmethod
    def _require_proposal_status(status: EpistemicStatus) -> None:
        if status not in {
            EpistemicStatus.IMPORTED,
            EpistemicStatus.INFERRED,
            EpistemicStatus.UNKNOWN,
        }:
            raise ValueError("Proposals must be IMPORTED, INFERRED, or UNKNOWN evidence")


def _validate_metric_expression(value: str, dialect: str) -> tuple[str, tuple[str, ...]]:
    expression = _parse_fragment(value, dialect, metric=True)
    if next(expression.find_all(exp.AggFunc), None) is None:
        raise AnalyticSemanticValidationError(
            "Metric expression must contain at least one aggregate function"
        )
    if next(expression.find_all(exp.Predicate, exp.Filter, exp.Case), None) is not None:
        raise AnalyticSemanticValidationError(
            "Metric filters and row predicates must be modeled as Business Rules"
        )
    return expression.sql(dialect=_sqlglot_dialect(dialect)), _column_refs(expression)


def _validate_rule_predicate(value: str, dialect: str) -> tuple[str, tuple[str, ...]]:
    predicate = _parse_fragment(value, dialect, metric=False)
    if next(predicate.find_all(exp.AggFunc), None) is not None:
        raise AnalyticSemanticValidationError(
            "Business Rules must be row predicates without aggregate functions"
        )
    if not any(
        isinstance(node, (exp.Predicate, exp.And, exp.Or, exp.Not))
        for node in predicate.walk()
    ):
        raise AnalyticSemanticValidationError("Business rule must be a boolean predicate")
    return predicate.sql(dialect=_sqlglot_dialect(dialect)), _column_refs(predicate)


def _parse_fragment(value: str, dialect: str, *, metric: bool) -> exp.Expression:
    if not value.strip() or len(value) > 20_000:
        raise AnalyticSemanticValidationError("SQL fragment must contain 1 to 20000 characters")
    wrapper = f"SELECT {value} AS __value" if metric else f"SELECT 1 WHERE {value}"
    try:
        statements = sqlglot.parse(wrapper, read=_sqlglot_dialect(dialect))
    except ParseError as error:
        raise AnalyticSemanticValidationError(f"Invalid SQL fragment: {error}") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise AnalyticSemanticValidationError("SQL fragment must produce one expression")
    statement = statements[0]
    if metric:
        projection = statement.expressions[0]
        candidate = projection.this if isinstance(projection, exp.Alias) else projection
    else:
        where = statement.args.get("where")
        if not isinstance(where, exp.Where):
            raise AnalyticSemanticValidationError("Business rule predicate is missing")
        candidate = where.this
    if not isinstance(candidate, exp.Expression):
        raise AnalyticSemanticValidationError("SQL fragment expression is invalid")
    expression = candidate
    prohibited = next(
        expression.find_all(
            exp.Select,
            exp.Subquery,
            exp.Table,
            exp.Star,
            exp.Parameter,
            exp.Placeholder,
            exp.Alias,
            exp.Window,
        ),
        None,
    )
    if prohibited is not None:
        raise AnalyticSemanticValidationError(
            f"SQL fragment contains prohibited {type(prohibited).__name__}"
        )
    for function in expression.find_all(exp.Anonymous):
        if function.name.casefold() not in _ALLOWED_ANONYMOUS_FUNCTIONS:
            raise AnalyticSemanticValidationError(
                f"Function {function.name.casefold()} is not allowlisted"
            )
    references = _column_refs(expression)
    if not references:
        raise AnalyticSemanticValidationError(
            "SQL fragment must reference at least one fully qualified column"
        )
    return expression


def _column_refs(expression: exp.Expression) -> tuple[str, ...]:
    references: set[str] = set()
    for column in expression.find_all(exp.Column):
        if column.catalog or not column.db or not column.table or not column.name:
            raise AnalyticSemanticValidationError(
                "Columns must use schema.table.column qualification"
            )
        references.add(f"{column.db}.{column.table}.{column.name}")
    return tuple(sorted(references))


def _sqlglot_dialect(value: str) -> str:
    try:
        return sqlglot_dialect_name(value)
    except UnsupportedDialectError as error:
        raise AnalyticSemanticValidationError(
            f"Governed metric and rule fragments do not support {value}"
        ) from error


def _kind(asset: AnalyticSemanticDefinition | AnalyticSemanticResolution) -> AnalyticSemanticKind:
    if isinstance(asset, (MetricDefinition, MetricResolution)):
        return AnalyticSemanticKind.METRIC
    return AnalyticSemanticKind.BUSINESS_RULE


def _key(asset: AnalyticSemanticDefinition | AnalyticSemanticResolution) -> str:
    if isinstance(asset, (MetricDefinition, MetricResolution)):
        return asset.metric_key
    return asset.rule_key


def _clean_key(value: str) -> str:
    result = value.strip()
    if result != result.casefold():
        raise ValueError("Analytic semantic keys must be lowercase")
    return result


def _canonical_keys(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(value.strip() for value in values)
    if any(value != value.casefold() for value in result):
        raise ValueError("Dependency keys must be lowercase")
    if len(result) != len(set(result)):
        raise ValueError("Dependency keys must be unique")
    return result


def _canonical_values(values: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(value.strip() for value in values)
    if any(not value for value in result) or len(result) != len(set(result)):
        raise ValueError("Analytic column references must be non-blank and unique")
    return result


def _matched_terms(normalized_query: str, name: str, key: str) -> tuple[str, ...]:
    candidates = {_normalize_term(name), _normalize_term(key.replace("_", " "))}
    return tuple(sorted(term for term in candidates if f" {term} " in normalized_query))


_ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "age",
        "date_bin",
        "date_part",
        "make_date",
        "split_part",
        "timezone",
        "width_bucket",
    }
)
