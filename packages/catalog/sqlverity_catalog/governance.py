from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.domain.sqlverity_domain.models import (
    CatalogVersion,
    EpistemicStatus,
    SemanticDefinition,
    SemanticResolution,
)

from .explorer import CatalogNotIngestedError
from .ingestion import DataSourceNotFoundError
from .repository import SemanticResolutionConflictError, SQLiteCatalogRepository


class SemanticObjectNotFoundError(LookupError):
    pass


class SemanticConcurrencyError(RuntimeError):
    pass


class SemanticDescriptionRequiredError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SemanticEvidenceEntry:
    id: str
    catalog_version_id: str
    description: str
    status: EpistemicStatus
    source: str
    confidence: float
    actor_id: str | None
    reason: str | None
    created_at: datetime
    selected: bool


@dataclass(frozen=True, slots=True)
class SemanticReviewItem:
    object_ref: str
    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime
    evidence: tuple[SemanticEvidenceEntry, ...]


@dataclass(frozen=True, slots=True)
class SemanticCorrectionResult:
    definition: SemanticDefinition
    resolution: SemanticResolution


class SemanticGovernanceService:
    def __init__(self, repository: SQLiteCatalogRepository) -> None:
        self._repository = repository

    def list_review_queue(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[SemanticReviewItem, ...]:
        _, known_refs = self._latest_context(tenant_id, data_source_id)
        resolutions = self._repository.list_semantic_resolutions(
            tenant_id,
            data_source_id,
            frozenset({EpistemicStatus.INFERRED, EpistemicStatus.CONFLICTING}),
        )
        return tuple(
            SemanticReviewItem(
                object_ref=resolution.object_ref,
                description=resolution.description,
                status=resolution.status,
                confidence=resolution.confidence,
                updated_at=resolution.updated_at,
                evidence=self.history(tenant_id, data_source_id, resolution.object_ref),
            )
            for resolution in resolutions
            if resolution.object_ref in known_refs
        )

    def history(
        self,
        tenant_id: str,
        data_source_id: str,
        object_ref: str,
    ) -> tuple[SemanticEvidenceEntry, ...]:
        self._require_object_ref(tenant_id, data_source_id, object_ref)
        current = self._repository.get_semantic_resolution(
            tenant_id,
            data_source_id,
            object_ref,
        )
        return tuple(
            SemanticEvidenceEntry(
                id=definition.id,
                catalog_version_id=definition.catalog_version_id,
                description=definition.description,
                status=definition.status,
                source=definition.source,
                confidence=definition.confidence,
                actor_id=definition.actor_id,
                reason=definition.reason,
                created_at=definition.created_at,
                selected=(
                    current is not None and current.selected_definition_id == definition.id
                ),
            )
            for definition in self._repository.list_semantic_definitions(
                tenant_id,
                data_source_id,
                object_ref,
            )
        )

    def correct(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        object_ref: str,
        actor_id: str,
        description: str | None,
        reason: str | None = None,
        expected_updated_at: datetime | None = None,
    ) -> SemanticCorrectionResult:
        version, known_refs = self._latest_context(tenant_id, data_source_id)
        if object_ref not in known_refs:
            raise SemanticObjectNotFoundError(
                f"Semantic object {object_ref} is not in the latest catalog version"
            )
        current = self._repository.get_semantic_resolution(
            tenant_id,
            data_source_id,
            object_ref,
        )
        final_description = description.strip() if description is not None else ""
        if not final_description and current is not None:
            final_description = current.description
        if not final_description:
            raise SemanticDescriptionRequiredError(
                "A description is required when no semantic resolution exists"
            )
        definition = SemanticDefinition(
            tenant_id=tenant_id,
            catalog_version_id=version.id,
            object_ref=object_ref,
            description=final_description,
            status=EpistemicStatus.CONFIRMED,
            source="human_correction",
            confidence=1.0,
            actor_id=actor_id,
            reason=reason,
        )
        try:
            result = self._repository.propose_semantic_definition(
                definition,
                explicit_supersede=True,
                expected_updated_at=expected_updated_at,
            )
        except SemanticResolutionConflictError as error:
            raise SemanticConcurrencyError(str(error)) from error
        return SemanticCorrectionResult(
            definition=result.evidence,
            resolution=result.resolution,
        )

    def _latest_context(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> tuple[CatalogVersion, frozenset[str]]:
        if self._repository.get_data_source(tenant_id, data_source_id) is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
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

    def _require_object_ref(
        self,
        tenant_id: str,
        data_source_id: str,
        object_ref: str,
    ) -> None:
        _, known_refs = self._latest_context(tenant_id, data_source_id)
        if object_ref not in known_refs:
            raise SemanticObjectNotFoundError(
                f"Semantic object {object_ref} is not in the latest catalog version"
            )
