from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from packages.catalog.sqlverity_catalog.analytics_semantics import (
    AnalyticsSemanticsService,
    BusinessRuleMatch,
    MetricMatch,
)
from packages.catalog.sqlverity_catalog.business_concepts import (
    BusinessConceptMatch,
    BusinessConceptService,
)
from packages.catalog.sqlverity_catalog.explorer import CatalogNotIngestedError
from packages.catalog.sqlverity_catalog.ingestion import DataSourceNotFoundError
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import (
    Classification,
    ColumnDefinition,
    EpistemicStatus,
    ObjectKind,
    Relationship,
    SchemaObject,
    SemanticResolution,
)
from packages.learning.sqlverity_learning import LearningLoopService, SQLExampleMatch


class ContextBuilderError(RuntimeError):
    pass


class ContextNoMatchesError(ContextBuilderError):
    pass


@dataclass(frozen=True, slots=True)
class ContextSemanticEntry:
    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ContextColumn:
    name: str
    physical_type: str
    nullable: bool
    classification: Classification
    is_primary_key: bool
    semantics: ContextSemanticEntry | None
    selection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextSchemaObject:
    id: str
    reference: str
    kind: ObjectKind
    lexical_score: int
    graph_expanded: bool
    selection_reasons: tuple[str, ...]
    semantics: ContextSemanticEntry | None
    columns: tuple[ContextColumn, ...]
    omitted_column_count: int


@dataclass(frozen=True, slots=True)
class ContextRelationship:
    name: str
    source_object_ref: str
    target_object_ref: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    status: EpistemicStatus
    confidence: float


@dataclass(frozen=True, slots=True)
class ContextSQLExample:
    id: str
    catalog_version_id: str
    question: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    referenced_columns: tuple[str, ...]
    business_concepts: tuple[str, ...]
    revision: int
    score: float
    classification: Classification


@dataclass(frozen=True, slots=True)
class ContextBusinessConcept:
    concept_key: str
    name: str
    description: str
    synonyms: tuple[str, ...]
    object_refs: tuple[str, ...]
    matched_terms: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


@dataclass(frozen=True, slots=True)
class ContextBusinessTermAmbiguity:
    term: str
    concept_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextMetric:
    metric_key: str
    name: str
    description: str
    normalized_expression_sql: str
    object_refs: tuple[str, ...]
    grain_refs: tuple[str, ...]
    dimension_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    rule_keys: tuple[str, ...]
    matched_terms: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


@dataclass(frozen=True, slots=True)
class ContextBusinessRule:
    rule_key: str
    name: str
    description: str
    normalized_predicate_sql: str
    object_refs: tuple[str, ...]
    concept_keys: tuple[str, ...]
    matched_terms: tuple[str, ...]
    selected_by_metrics: tuple[str, ...]
    status: EpistemicStatus
    confidence: float
    classification: Classification


@dataclass(frozen=True, slots=True)
class SchemaContextSnapshot:
    data_source_id: str
    catalog_version_id: str
    catalog_version: int
    dialect: str
    query: str
    query_terms: tuple[str, ...]
    selection_strategy: str
    objects: tuple[ContextSchemaObject, ...]
    relationships: tuple[ContextRelationship, ...]
    matched_seed_count: int
    omitted_object_count: int
    sql_examples: tuple[ContextSQLExample, ...] = ()
    business_concepts: tuple[ContextBusinessConcept, ...] = ()
    business_term_ambiguities: tuple[ContextBusinessTermAmbiguity, ...] = ()
    metrics: tuple[ContextMetric, ...] = ()
    business_rules: tuple[ContextBusinessRule, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    schema_object: SchemaObject
    columns: tuple[ColumnDefinition, ...]
    semantics: SemanticResolution | None
    score: int
    reasons: tuple[str, ...]


class ContextBuilderService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        learning_loop: LearningLoopService | None = None,
        business_concepts: BusinessConceptService | None = None,
        analytics_semantics: AnalyticsSemanticsService | None = None,
    ) -> None:
        self._repository = repository
        self._learning_loop = learning_loop
        self._business_concepts = business_concepts
        self._analytics_semantics = analytics_semantics

    def build(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        query: str,
        max_seed_objects: int = 5,
        max_objects: int = 12,
        graph_hops: int = 1,
        target_columns_per_object: int = 20,
        max_sql_examples: int = 3,
    ) -> SchemaContextSnapshot:
        self._validate_limits(
            max_seed_objects,
            max_objects,
            graph_hops,
            target_columns_per_object,
            max_sql_examples,
        )
        query = query.strip()
        if not query or len(query) > 10_000:
            raise ValueError("Context query must contain between 1 and 10000 characters")
        query_terms = _tokenize(query)
        if not query_terms:
            raise ContextNoMatchesError("Context query has no searchable terms")

        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        version = self._repository.get_latest_catalog_version(tenant_id, data_source_id)
        if version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")

        term_resolution = (
            self._business_concepts.resolve_terms(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                query=query,
            )
            if self._business_concepts is not None
            else None
        )
        concept_matches = term_resolution.matches if term_resolution is not None else ()
        analytic_context = (
            self._analytics_semantics.resolve_for_query(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                query=query,
                concept_keys=frozenset(
                    match.resolution.concept_key for match in concept_matches
                ),
            )
            if self._analytics_semantics is not None
            else None
        )
        metric_matches = analytic_context.metrics if analytic_context is not None else ()
        rule_matches = (
            analytic_context.business_rules if analytic_context is not None else ()
        )

        schema_objects = self._repository.list_schema_objects(tenant_id, version.id)
        columns_by_object: dict[str, list[ColumnDefinition]] = {}
        for column in self._repository.list_columns_for_catalog_version(
            tenant_id,
            version.id,
        ):
            columns_by_object.setdefault(column.schema_object_id, []).append(column)
        confirmed_semantics = {
            resolution.object_ref: resolution
            for resolution in self._repository.list_semantic_resolutions(
                tenant_id,
                data_source_id,
                frozenset({EpistemicStatus.CONFIRMED}),
            )
        }
        candidates = tuple(
            self._candidate(
                schema_object,
                tuple(columns_by_object.get(schema_object.id, ())),
                query,
                query_terms,
                confirmed_semantics,
            )
            for schema_object in schema_objects
        )
        ranked = tuple(
            sorted(
                (candidate for candidate in candidates if candidate.score > 0),
                key=lambda candidate: (-candidate.score, candidate.schema_object.reference),
            )
        )
        example_matches = self._example_matches(
            tenant_id,
            data_source_id,
            query,
            max_sql_examples,
        )
        candidates_by_reference = {
            candidate.schema_object.reference: candidate for candidate in candidates
        }
        seed_ids: list[str] = []
        concept_seed_reasons: dict[str, set[str]] = {}
        concept_columns: set[str] = set()
        for concept_match in concept_matches:
            for object_ref in concept_match.resolution.object_refs:
                table_ref = (
                    object_ref
                    if object_ref in candidates_by_reference
                    else object_ref.rsplit(".", 1)[0]
                )
                candidate = candidates_by_reference.get(table_ref)
                if candidate is None:
                    continue
                if object_ref != table_ref:
                    concept_columns.add(object_ref)
                concept_seed_reasons.setdefault(candidate.schema_object.id, set()).add(
                    concept_match.resolution.concept_key
                )
                if (
                    candidate.schema_object.id not in seed_ids
                    and len(seed_ids) < max_seed_objects
                ):
                    seed_ids.append(candidate.schema_object.id)
        analytic_seed_reasons: dict[str, set[str]] = {}
        analytic_columns: set[str] = set()
        analytic_assets = tuple(
            (f"metric:{item.resolution.metric_key}", item.resolution.object_refs)
            for item in metric_matches
        ) + tuple(
            (f"business_rule:{item.resolution.rule_key}", item.resolution.object_refs)
            for item in rule_matches
        )
        for reason, object_refs in analytic_assets:
            for object_ref in object_refs:
                table_ref = object_ref.rsplit(".", 1)[0]
                candidate = candidates_by_reference.get(table_ref)
                if candidate is None:
                    continue
                analytic_columns.add(object_ref)
                analytic_seed_reasons.setdefault(candidate.schema_object.id, set()).add(reason)
                if (
                    candidate.schema_object.id not in seed_ids
                    and len(seed_ids) < max_seed_objects
                ):
                    seed_ids.append(candidate.schema_object.id)
        example_seed_reasons: dict[str, str] = {}
        for example_match in example_matches:
            for table_ref in example_match.example.referenced_tables:
                candidate = candidates_by_reference.get(table_ref)
                if candidate is None or candidate.schema_object.id in seed_ids:
                    continue
                seed_ids.append(candidate.schema_object.id)
                example_seed_reasons[candidate.schema_object.id] = (
                    f"corrected_sql_example:{example_match.example.id}"
                )
                if len(seed_ids) >= max_seed_objects:
                    break
            if len(seed_ids) >= max_seed_objects:
                break
        for candidate in ranked:
            if candidate.schema_object.id not in seed_ids:
                seed_ids.append(candidate.schema_object.id)
            if len(seed_ids) >= max_seed_objects:
                break
        if not seed_ids:
            raise ContextNoMatchesError("No schema objects match the context query")

        relationships = tuple(
            relationship
            for relationship in self._repository.list_relationships(tenant_id, version.id)
            if relationship.status
            not in {EpistemicStatus.UNKNOWN, EpistemicStatus.CONFLICTING}
        )
        selected_ids, expansion_reasons = _expand_relationship_graph(
            tuple(seed_ids),
            relationships,
            max_objects=max_objects,
            graph_hops=graph_hops,
        )
        candidates_by_id = {
            candidate.schema_object.id: candidate for candidate in candidates
        }
        seed_id_set = frozenset(seed_ids)
        included_relationships = tuple(
            relationship
            for relationship in relationships
            if relationship.source_object_id in selected_ids
            and relationship.target_object_id in selected_ids
        )
        references = {
            candidate.schema_object.id: candidate.schema_object.reference
            for candidate in candidates
        }
        example_columns = frozenset(
            column
            for match in example_matches
            for column in match.example.referenced_columns
        )
        objects = tuple(
            self._context_object(
                candidate=candidates_by_id[object_id],
                query_terms=query_terms,
                relationships=included_relationships,
                graph_expanded=object_id not in seed_id_set,
                expansion_reason=expansion_reasons.get(object_id),
                example_reason=example_seed_reasons.get(object_id),
                example_columns=example_columns,
                concept_reasons=tuple(
                    sorted(concept_seed_reasons.get(object_id, set()))
                ),
                concept_columns=frozenset(concept_columns),
                analytic_reasons=tuple(
                    sorted(analytic_seed_reasons.get(object_id, set()))
                ),
                analytic_columns=frozenset(analytic_columns),
                confirmed_semantics=confirmed_semantics,
                target_columns=target_columns_per_object,
            )
            for object_id in selected_ids
        )
        return SchemaContextSnapshot(
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            catalog_version=version.version,
            dialect=data_source.dialect,
            query=query,
            query_terms=tuple(sorted(query_terms)),
            selection_strategy=_selection_strategy(
                business_concepts=self._business_concepts is not None,
                corrected_sql=self._learning_loop is not None,
                analytics_semantics=self._analytics_semantics is not None,
            ),
            objects=objects,
            relationships=tuple(
                ContextRelationship(
                    name=relationship.name,
                    source_object_ref=references[relationship.source_object_id],
                    target_object_ref=references[relationship.target_object_id],
                    source_columns=relationship.source_columns,
                    target_columns=relationship.target_columns,
                    status=relationship.status,
                    confidence=relationship.confidence,
                )
                for relationship in included_relationships
            ),
            matched_seed_count=len(seed_ids),
            omitted_object_count=len(schema_objects) - len(objects),
            sql_examples=self._context_examples(example_matches, objects),
            business_concepts=self._context_business_concepts(concept_matches, objects),
            business_term_ambiguities=tuple(
                ContextBusinessTermAmbiguity(
                    term=item.term,
                    concept_keys=item.concept_keys,
                )
                for item in (
                    term_resolution.ambiguities if term_resolution is not None else ()
                )
            ),
            metrics=self._context_metrics(metric_matches, rule_matches, objects),
            business_rules=self._context_business_rules(rule_matches, objects),
        )

    def _candidate(
        self,
        schema_object: SchemaObject,
        columns: tuple[ColumnDefinition, ...],
        query: str,
        query_terms: frozenset[str],
        confirmed_semantics: Mapping[str, SemanticResolution],
    ) -> _Candidate:
        semantics = confirmed_semantics.get(schema_object.reference)
        score = 0
        reasons: set[str] = set()

        object_overlap = query_terms & _tokenize(schema_object.reference)
        if object_overlap:
            score += 20 * len(object_overlap)
            reasons.add("object_name")
        normalized_object_name = _normalized_phrase(schema_object.name)
        if normalized_object_name and normalized_object_name in _normalized_phrase(query):
            score += 30
            reasons.add("exact_object_name")
        if semantics is not None:
            semantic_overlap = query_terms & _tokenize(semantics.description)
            if semantic_overlap:
                score += 8 * len(semantic_overlap)
                reasons.add("confirmed_object_description")

        for column in columns:
            column_ref = f"{schema_object.reference}.{column.name}"
            column_overlap = query_terms & _tokenize(column.name)
            if column_overlap:
                score += 10 * len(column_overlap)
                reasons.add(f"column:{column.name}")
            column_semantics = confirmed_semantics.get(column_ref)
            if column_semantics is not None:
                description_overlap = query_terms & _tokenize(column_semantics.description)
                if description_overlap:
                    score += 4 * len(description_overlap)
                    reasons.add(f"confirmed_column_description:{column.name}")

        return _Candidate(
            schema_object=schema_object,
            columns=columns,
            semantics=semantics,
            score=score,
            reasons=tuple(sorted(reasons)),
        )

    def _context_object(
        self,
        *,
        candidate: _Candidate,
        query_terms: frozenset[str],
        relationships: tuple[Relationship, ...],
        graph_expanded: bool,
        expansion_reason: str | None,
        example_reason: str | None,
        example_columns: frozenset[str],
        concept_reasons: tuple[str, ...],
        concept_columns: frozenset[str],
        analytic_reasons: tuple[str, ...],
        analytic_columns: frozenset[str],
        confirmed_semantics: Mapping[str, SemanticResolution],
        target_columns: int,
    ) -> ContextSchemaObject:
        relationship_columns: set[str] = set()
        for relationship in relationships:
            if relationship.source_object_id == candidate.schema_object.id:
                relationship_columns.update(relationship.source_columns)
            if relationship.target_object_id == candidate.schema_object.id:
                relationship_columns.update(relationship.target_columns)

        ranked_columns: list[tuple[int, int, ColumnDefinition, tuple[str, ...]]] = []
        for column in candidate.columns:
            reasons: set[str] = set()
            score = 0
            if column.is_primary_key:
                score += 100
                reasons.add("primary_key")
            if column.name in relationship_columns:
                score += 90
                reasons.add("relationship_key")
            if f"{candidate.schema_object.reference}.{column.name}" in example_columns:
                score += 80
                reasons.add("corrected_sql_example")
            if f"{candidate.schema_object.reference}.{column.name}" in concept_columns:
                score += 85
                reasons.add("business_concept")
            if f"{candidate.schema_object.reference}.{column.name}" in analytic_columns:
                score += 88
                reasons.add("analytic_semantic")
            overlap = query_terms & _tokenize(column.name)
            if overlap:
                score += 30 * len(overlap)
                reasons.add("query_match")
            column_semantics = confirmed_semantics.get(
                f"{candidate.schema_object.reference}.{column.name}"
            )
            if column_semantics is not None and query_terms & _tokenize(
                column_semantics.description
            ):
                score += 15
                reasons.add("confirmed_description_match")
            ranked_columns.append((score, column.ordinal, column, tuple(sorted(reasons))))

        essential = [entry for entry in ranked_columns if entry[0] > 0]
        other = [entry for entry in ranked_columns if entry[0] == 0]
        essential.sort(key=lambda entry: (-entry[0], entry[1]))
        other.sort(key=lambda entry: (-entry[0], entry[1]))
        selected = essential + other[: max(0, target_columns - len(essential))]
        selected.sort(key=lambda entry: entry[1])
        context_columns = tuple(
            ContextColumn(
                name=column.name,
                physical_type=column.physical_type,
                nullable=column.nullable,
                classification=column.classification,
                is_primary_key=column.is_primary_key,
                semantics=self._semantic_entry(
                    confirmed_semantics.get(
                        f"{candidate.schema_object.reference}.{column.name}",
                    )
                ),
                selection_reasons=reasons or ("schema_context",),
            )
            for _, _, column, reasons in selected
        )
        selection_reasons = list(candidate.reasons)
        if expansion_reason is not None:
            selection_reasons.append(expansion_reason)
        if example_reason is not None:
            selection_reasons.append(example_reason)
        selection_reasons.extend(
            f"business_concept:{concept_key}" for concept_key in concept_reasons
        )
        selection_reasons.extend(analytic_reasons)
        return ContextSchemaObject(
            id=candidate.schema_object.id,
            reference=candidate.schema_object.reference,
            kind=candidate.schema_object.kind,
            lexical_score=candidate.score,
            graph_expanded=graph_expanded,
            selection_reasons=tuple(selection_reasons),
            semantics=self._semantic_entry(candidate.semantics),
            columns=context_columns,
            omitted_column_count=len(candidate.columns) - len(context_columns),
        )

    @staticmethod
    def _semantic_entry(
        resolution: SemanticResolution | None,
    ) -> ContextSemanticEntry | None:
        if resolution is None:
            return None
        return ContextSemanticEntry(
            description=resolution.description,
            status=resolution.status,
            confidence=resolution.confidence,
            updated_at=resolution.updated_at,
        )

    @staticmethod
    def _validate_limits(
        max_seed_objects: int,
        max_objects: int,
        graph_hops: int,
        target_columns_per_object: int,
        max_sql_examples: int,
    ) -> None:
        if not 1 <= max_seed_objects <= 20:
            raise ValueError("max_seed_objects must be between 1 and 20")
        if not max_seed_objects <= max_objects <= 50:
            raise ValueError("max_objects must be between max_seed_objects and 50")
        if not 0 <= graph_hops <= 3:
            raise ValueError("graph_hops must be between 0 and 3")
        if not 1 <= target_columns_per_object <= 100:
            raise ValueError("target_columns_per_object must be between 1 and 100")
        if not 0 <= max_sql_examples <= 10:
            raise ValueError("max_sql_examples must be between zero and ten")

    def _example_matches(
        self,
        tenant_id: str,
        data_source_id: str,
        query: str,
        max_sql_examples: int,
    ) -> tuple[SQLExampleMatch, ...]:
        if self._learning_loop is None:
            return ()
        return self._learning_loop.retrieve(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            question=query,
            max_results=max_sql_examples,
        )

    @staticmethod
    def _context_examples(
        matches: tuple[SQLExampleMatch, ...],
        objects: tuple[ContextSchemaObject, ...],
    ) -> tuple[ContextSQLExample, ...]:
        selected_tables = frozenset(item.reference for item in objects)
        column_classifications = {
            f"{schema_object.reference}.{column.name}": column.classification
            for schema_object in objects
            for column in schema_object.columns
        }
        selected_columns = frozenset(column_classifications)
        examples: list[ContextSQLExample] = []
        for match in matches:
            example = match.example
            if not set(example.referenced_tables).issubset(selected_tables):
                continue
            if not set(example.referenced_columns).issubset(selected_columns):
                continue
            classifications = (
                example.content_classification,
                *(
                    column_classifications[column]
                    for column in example.referenced_columns
                ),
            )
            examples.append(
                ContextSQLExample(
                    id=example.id,
                    catalog_version_id=example.catalog_version_id,
                    question=example.question,
                    normalized_sql=example.normalized_sql,
                    referenced_tables=example.referenced_tables,
                    referenced_columns=example.referenced_columns,
                    business_concepts=example.business_concepts,
                    revision=example.revision,
                    score=match.score,
                    classification=max(classifications, key=_classification_rank),
                )
            )
        return tuple(examples)

    @staticmethod
    def _context_business_concepts(
        matches: tuple[BusinessConceptMatch, ...],
        objects: tuple[ContextSchemaObject, ...],
    ) -> tuple[ContextBusinessConcept, ...]:
        selected_tables = frozenset(item.reference for item in objects)
        column_classifications = {
            f"{schema_object.reference}.{column.name}": column.classification
            for schema_object in objects
            for column in schema_object.columns
        }
        selected_refs = selected_tables | frozenset(column_classifications)
        concepts: list[ContextBusinessConcept] = []
        for match in matches:
            resolution = match.resolution
            if not set(resolution.object_refs).issubset(selected_refs):
                continue
            classifications = (
                resolution.content_classification,
                *(
                    column_classifications[object_ref]
                    for object_ref in resolution.object_refs
                    if object_ref in column_classifications
                ),
            )
            concepts.append(
                ContextBusinessConcept(
                    concept_key=resolution.concept_key,
                    name=resolution.name,
                    description=resolution.description,
                    synonyms=resolution.synonyms,
                    object_refs=resolution.object_refs,
                    matched_terms=match.matched_terms,
                    status=resolution.status,
                    confidence=resolution.confidence,
                    classification=max(classifications, key=_classification_rank),
                )
            )
        return tuple(concepts)

    @staticmethod
    def _context_metrics(
        matches: tuple[MetricMatch, ...],
        rule_matches: tuple[BusinessRuleMatch, ...],
        objects: tuple[ContextSchemaObject, ...],
    ) -> tuple[ContextMetric, ...]:
        selected_refs, classifications = _selected_context_refs(objects)
        metrics: list[ContextMetric] = []
        rules_by_key = {
            match.resolution.rule_key: match.resolution for match in rule_matches
        }
        for match in matches:
            resolution = match.resolution
            dependent_rules = tuple(
                rules_by_key[key] for key in resolution.rule_keys if key in rules_by_key
            )
            if len(dependent_rules) != len(resolution.rule_keys):
                continue
            required_refs = set(resolution.object_refs)
            required_refs.update(
                object_ref
                for rule in dependent_rules
                for object_ref in rule.object_refs
            )
            if not required_refs.issubset(selected_refs):
                continue
            metric_classification = max(
                (
                    resolution.content_classification,
                    *(classifications[value] for value in required_refs),
                    *(rule.content_classification for rule in dependent_rules),
                ),
                key=_classification_rank,
            )
            metrics.append(
                ContextMetric(
                    metric_key=resolution.metric_key,
                    name=resolution.name,
                    description=resolution.description,
                    normalized_expression_sql=resolution.normalized_expression_sql,
                    object_refs=resolution.object_refs,
                    grain_refs=resolution.grain_refs,
                    dimension_refs=resolution.dimension_refs,
                    concept_keys=resolution.concept_keys,
                    rule_keys=resolution.rule_keys,
                    matched_terms=match.matched_terms,
                    status=resolution.status,
                    confidence=resolution.confidence,
                    classification=metric_classification,
                )
            )
        return tuple(metrics)

    @staticmethod
    def _context_business_rules(
        matches: tuple[BusinessRuleMatch, ...],
        objects: tuple[ContextSchemaObject, ...],
    ) -> tuple[ContextBusinessRule, ...]:
        selected_refs, classifications = _selected_context_refs(objects)
        rules: list[ContextBusinessRule] = []
        for match in matches:
            resolution = match.resolution
            if not set(resolution.object_refs).issubset(selected_refs):
                continue
            rule_classification = max(
                (
                    resolution.content_classification,
                    *(classifications[value] for value in resolution.object_refs),
                ),
                key=_classification_rank,
            )
            rules.append(
                ContextBusinessRule(
                    rule_key=resolution.rule_key,
                    name=resolution.name,
                    description=resolution.description,
                    normalized_predicate_sql=resolution.normalized_predicate_sql,
                    object_refs=resolution.object_refs,
                    concept_keys=resolution.concept_keys,
                    matched_terms=match.matched_terms,
                    selected_by_metrics=match.selected_by_metrics,
                    status=resolution.status,
                    confidence=resolution.confidence,
                    classification=rule_classification,
                )
            )
        return tuple(rules)


def _expand_relationship_graph(
    seed_ids: tuple[str, ...],
    relationships: tuple[Relationship, ...],
    *,
    max_objects: int,
    graph_hops: int,
) -> tuple[tuple[str, ...], dict[str, str]]:
    selected = list(seed_ids[:max_objects])
    selected_set = set(selected)
    expansion_reasons: dict[str, str] = {}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for relationship in relationships:
        adjacency.setdefault(relationship.source_object_id, []).append(
            (relationship.target_object_id, relationship.name)
        )
        adjacency.setdefault(relationship.target_object_id, []).append(
            (relationship.source_object_id, relationship.name)
        )
    frontier = deque((object_id, 0) for object_id in selected)
    while frontier and len(selected) < max_objects:
        object_id, depth = frontier.popleft()
        if depth >= graph_hops:
            continue
        for neighbor_id, relationship_name in sorted(adjacency.get(object_id, [])):
            if neighbor_id in selected_set:
                continue
            selected.append(neighbor_id)
            selected_set.add(neighbor_id)
            expansion_reasons[neighbor_id] = f"relationship:{relationship_name}"
            frontier.append((neighbor_id, depth + 1))
            if len(selected) >= max_objects:
                break
    return tuple(selected), expansion_reasons


_STOP_WORDS = frozenset(
    {
        "a",
        "and",
        "con",
        "da",
        "dei",
        "del",
        "della",
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
        "show",
        "the",
        "un",
        "una",
    }
)


def _tokenize(value: str) -> frozenset[str]:
    return frozenset(_ordered_tokens(value))


def _ordered_tokens(value: str) -> tuple[str, ...]:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    tokens = re.findall(r"\w+", camel_split.casefold(), flags=re.UNICODE)
    return tuple(token for token in tokens if token not in _STOP_WORDS)


def _normalized_phrase(value: str) -> str:
    return " ".join(_ordered_tokens(value))


def _classification_rank(value: Classification) -> int:
    return {
        Classification.PUBLIC: 0,
        Classification.INTERNAL: 1,
        Classification.CONFIDENTIAL: 2,
        Classification.PII: 3,
        Classification.HIGHLY_SENSITIVE: 4,
    }[value]


def _selection_strategy(
    *,
    business_concepts: bool,
    corrected_sql: bool,
    analytics_semantics: bool,
) -> str:
    parts: list[str] = []
    if business_concepts:
        parts.append("business_concept_v1")
    if corrected_sql:
        parts.append("corrected_sql_v1")
    if analytics_semantics:
        parts.append("analytic_semantics_v1")
    parts.extend(("lexical_v1", "relationship_bfs"))
    return "+".join(parts)


def _selected_context_refs(
    objects: tuple[ContextSchemaObject, ...],
) -> tuple[frozenset[str], dict[str, Classification]]:
    classifications = {
        f"{schema_object.reference}.{column.name}": column.classification
        for schema_object in objects
        for column in schema_object.columns
    }
    return frozenset(classifications), classifications
