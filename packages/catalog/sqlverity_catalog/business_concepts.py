from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.domain.sqlverity_domain.models import (
    BusinessConceptDefinition,
    BusinessConceptResolution,
    CatalogVersion,
    Classification,
    EpistemicStatus,
)
from packages.domain.sqlverity_domain.text import normalize_search_term as _normalize_term

from .explorer import CatalogNotIngestedError
from .ingestion import DataSourceNotFoundError
from .repository import (
    BusinessConceptResolutionConflictError,
    BusinessConceptWriteResult,
    SQLiteCatalogRepository,
)


class BusinessConceptNotFoundError(LookupError):
    pass


class BusinessConceptObjectNotFoundError(LookupError):
    pass


class BusinessConceptConcurrencyError(RuntimeError):
    pass


class BusinessTermConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BusinessConceptEvidenceEntry:
    definition: BusinessConceptDefinition
    selected: bool


@dataclass(frozen=True, slots=True)
class BusinessConceptReviewItem:
    resolution: BusinessConceptResolution
    evidence: tuple[BusinessConceptEvidenceEntry, ...]


@dataclass(frozen=True, slots=True)
class BusinessConceptMatch:
    resolution: BusinessConceptResolution
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessTermAmbiguity:
    term: str
    concept_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BusinessTermResolution:
    matches: tuple[BusinessConceptMatch, ...]
    ambiguities: tuple[BusinessTermAmbiguity, ...]


@dataclass(frozen=True, slots=True)
class BusinessConceptCorrectionResult:
    definition: BusinessConceptDefinition
    resolution: BusinessConceptResolution


class BusinessConceptService:
    def __init__(self, repository: SQLiteCatalogRepository) -> None:
        self._repository = repository

    def propose(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        concept_key: str,
        name: str,
        description: str,
        synonyms: tuple[str, ...],
        object_refs: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None = None,
        reason: str | None = None,
    ) -> BusinessConceptWriteResult:
        if status not in {
            EpistemicStatus.IMPORTED,
            EpistemicStatus.INFERRED,
            EpistemicStatus.UNKNOWN,
        }:
            raise ValueError("Proposals must be IMPORTED, INFERRED, or UNKNOWN evidence")
        version, known_refs = self._latest_context(tenant_id, data_source_id)
        normalized_refs = _canonical_values(object_refs)
        self._require_object_refs(normalized_refs, known_refs)
        definition = self._definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            version=version,
            concept_key=concept_key,
            name=name,
            description=description,
            synonyms=synonyms,
            object_refs=normalized_refs,
            content_classification=content_classification,
            status=status,
            source=source,
            confidence=confidence,
            actor_id=actor_id,
            reason=reason,
        )
        return self._repository.propose_business_concept_definition(definition)

    def correct(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        concept_key: str,
        name: str,
        description: str,
        synonyms: tuple[str, ...],
        object_refs: tuple[str, ...],
        content_classification: Classification,
        actor_id: str,
        reason: str | None = None,
        expected_updated_at: datetime | None = None,
    ) -> BusinessConceptCorrectionResult:
        version, known_refs = self._latest_context(tenant_id, data_source_id)
        normalized_refs = _canonical_values(object_refs)
        self._require_object_refs(normalized_refs, known_refs)
        definition = self._definition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            version=version,
            concept_key=concept_key,
            name=name,
            description=description,
            synonyms=synonyms,
            object_refs=normalized_refs,
            content_classification=content_classification,
            status=EpistemicStatus.CONFIRMED,
            source="human_correction",
            confidence=1.0,
            actor_id=actor_id,
            reason=reason,
        )
        self._require_unique_confirmed_terms(definition)
        try:
            result = self._repository.propose_business_concept_definition(
                definition,
                explicit_supersede=True,
                expected_updated_at=expected_updated_at,
            )
        except BusinessConceptResolutionConflictError as error:
            raise BusinessConceptConcurrencyError(str(error)) from error
        return BusinessConceptCorrectionResult(result.evidence, result.resolution)

    def list_concepts(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[BusinessConceptResolution, ...]:
        self._require_data_source(tenant_id, data_source_id)
        return self._repository.list_business_concept_resolutions(
            tenant_id,
            data_source_id,
        )

    def history(
        self,
        tenant_id: str,
        data_source_id: str,
        concept_key: str,
    ) -> tuple[BusinessConceptEvidenceEntry, ...]:
        self._require_data_source(tenant_id, data_source_id)
        current = self._repository.get_business_concept_resolution(
            tenant_id,
            data_source_id,
            concept_key,
        )
        if current is None:
            raise BusinessConceptNotFoundError("Business concept does not exist")
        return tuple(
            BusinessConceptEvidenceEntry(
                definition=definition,
                selected=current.selected_definition_id == definition.id,
            )
            for definition in self._repository.list_business_concept_definitions(
                tenant_id,
                data_source_id,
                concept_key,
            )
        )

    def list_review_queue(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[BusinessConceptReviewItem, ...]:
        self._require_data_source(tenant_id, data_source_id)
        resolutions = self._repository.list_business_concept_resolutions(
            tenant_id,
            data_source_id,
            frozenset({EpistemicStatus.INFERRED, EpistemicStatus.CONFLICTING}),
        )
        return tuple(
            BusinessConceptReviewItem(
                resolution=resolution,
                evidence=self.history(tenant_id, data_source_id, resolution.concept_key),
            )
            for resolution in resolutions
        )

    def resolve_terms(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        query: str,
    ) -> BusinessTermResolution:
        self._require_data_source(tenant_id, data_source_id)
        normalized_query = f" {_normalize_term(query)} "
        if not normalized_query.strip():
            raise ValueError("Business term query must not be blank")
        concepts = self._repository.list_business_concept_resolutions(
            tenant_id,
            data_source_id,
            frozenset({EpistemicStatus.CONFIRMED}),
        )
        term_map: dict[str, list[BusinessConceptResolution]] = {}
        for concept in concepts:
            for term in _concept_terms(concept):
                term_map.setdefault(term, []).append(concept)
        matched = {
            term: values
            for term, values in term_map.items()
            if f" {term} " in normalized_query
        }
        ambiguities = tuple(
            BusinessTermAmbiguity(
                term=term,
                concept_keys=tuple(sorted(item.concept_key for item in values)),
            )
            for term, values in sorted(matched.items())
            if len(values) > 1
        )
        ambiguous_terms = frozenset(item.term for item in ambiguities)
        matches_by_key: dict[str, tuple[BusinessConceptResolution, list[str]]] = {}
        for term, values in matched.items():
            if term in ambiguous_terms:
                continue
            concept = values[0]
            entry = matches_by_key.setdefault(concept.concept_key, (concept, []))
            entry[1].append(term)
        matches = tuple(
            BusinessConceptMatch(
                resolution=concept,
                matched_terms=tuple(sorted(terms, key=lambda value: (-len(value), value))),
            )
            for concept, terms in sorted(
                matches_by_key.values(),
                key=lambda entry: entry[0].concept_key,
            )
        )
        return BusinessTermResolution(matches=matches, ambiguities=ambiguities)

    def _definition(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        version: CatalogVersion,
        concept_key: str,
        name: str,
        description: str,
        synonyms: tuple[str, ...],
        object_refs: tuple[str, ...],
        content_classification: Classification,
        status: EpistemicStatus,
        source: str,
        confidence: float,
        actor_id: str | None,
        reason: str | None,
    ) -> BusinessConceptDefinition:
        clean_key = concept_key.strip()
        if clean_key != clean_key.casefold():
            raise ValueError("Business concept key must be lowercase")
        clean_name = name.strip()
        clean_synonyms = _canonical_values(synonyms, normalized_unique=True)
        if _normalize_term(clean_name) in {
            _normalize_term(value) for value in clean_synonyms
        }:
            raise ValueError("Business concept name must not be repeated as a synonym")
        return BusinessConceptDefinition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            concept_key=clean_key,
            name=clean_name,
            description=description.strip(),
            synonyms=clean_synonyms,
            object_refs=object_refs,
            content_classification=content_classification,
            status=status,
            source=source.strip(),
            confidence=confidence,
            actor_id=actor_id.strip() if actor_id is not None else None,
            reason=reason.strip() if reason is not None else None,
        )

    def _latest_context(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[CatalogVersion, frozenset[str]]:
        self._require_data_source(tenant_id, data_source_id)
        version = self._repository.get_latest_catalog_version(tenant_id, data_source_id)
        if version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")
        schema_objects = self._repository.list_schema_objects(tenant_id, version.id)
        objects_by_id = {schema_object.id: schema_object for schema_object in schema_objects}
        references = {schema_object.reference for schema_object in schema_objects}
        references.update(
            f"{objects_by_id[column.schema_object_id].reference}.{column.name}"
            for column in self._repository.list_columns_for_catalog_version(
                tenant_id,
                version.id,
            )
        )
        return version, frozenset(references)

    def _require_data_source(self, tenant_id: str, data_source_id: str) -> None:
        if self._repository.get_data_source(tenant_id, data_source_id) is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")

    @staticmethod
    def _require_object_refs(
        object_refs: tuple[str, ...],
        known_refs: frozenset[str],
    ) -> None:
        missing = tuple(value for value in object_refs if value not in known_refs)
        if missing:
            raise BusinessConceptObjectNotFoundError(
                f"Business concept references unknown catalog objects: {', '.join(missing)}"
            )

    def _require_unique_confirmed_terms(
        self,
        candidate: BusinessConceptDefinition,
    ) -> None:
        candidate_terms = frozenset(_concept_terms(candidate))
        collisions: dict[str, list[str]] = {}
        for current in self._repository.list_business_concept_resolutions(
            candidate.tenant_id,
            candidate.data_source_id,
            frozenset({EpistemicStatus.CONFIRMED}),
        ):
            if current.concept_key == candidate.concept_key:
                continue
            for term in candidate_terms & frozenset(_concept_terms(current)):
                collisions.setdefault(term, []).append(current.concept_key)
        if collisions:
            details = "; ".join(
                f"{term}: {', '.join(sorted(keys))}"
                for term, keys in sorted(collisions.items())
            )
            raise BusinessTermConflictError(
                f"Confirmed business terms already belong to another concept ({details})"
            )


def _concept_terms(
    concept: BusinessConceptDefinition | BusinessConceptResolution,
) -> tuple[str, ...]:
    raw_terms = (concept.name, concept.concept_key.replace("_", " "), *concept.synonyms)
    return tuple(sorted({_normalize_term(value) for value in raw_terms if value.strip()}))


def _canonical_values(
    values: tuple[str, ...],
    *,
    normalized_unique: bool = False,
) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = raw_value.strip()
        identity = _normalize_term(value) if normalized_unique else value
        if identity in seen:
            raise ValueError("Business concept list values must be unique")
        seen.add(identity)
        result.append(value)
    return tuple(result)
