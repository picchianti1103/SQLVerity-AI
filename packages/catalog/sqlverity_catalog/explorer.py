from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.domain.sqlverity_domain.models import (
    Classification,
    ColumnDefinition,
    EpistemicStatus,
    ObjectKind,
    SemanticResolution,
)

from .ingestion import DataSourceNotFoundError
from .repository import SQLiteCatalogRepository


class CatalogNotIngestedError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class SemanticExplorerEntry:
    description: str
    status: EpistemicStatus
    confidence: float
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ColumnExplorerEntry:
    name: str
    physical_type: str
    ordinal: int
    nullable: bool
    classification: Classification
    default_expression: str | None
    is_primary_key: bool
    semantics: SemanticExplorerEntry | None


@dataclass(frozen=True, slots=True)
class ObjectExplorerEntry:
    id: str
    reference: str
    schema_name: str
    name: str
    kind: ObjectKind
    definition_sql: str | None
    semantics: SemanticExplorerEntry | None
    columns: tuple[ColumnExplorerEntry, ...]


@dataclass(frozen=True, slots=True)
class RelationshipExplorerEntry:
    name: str
    source_object_ref: str
    target_object_ref: str
    source_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    status: EpistemicStatus
    confidence: float


@dataclass(frozen=True, slots=True)
class SchemaExplorerSnapshot:
    data_source_id: str
    catalog_version_id: str
    catalog_version: int
    created_at: datetime
    objects: tuple[ObjectExplorerEntry, ...]
    relationships: tuple[RelationshipExplorerEntry, ...]


class SchemaExplorerService:
    def __init__(self, repository: SQLiteCatalogRepository) -> None:
        self._repository = repository

    def get_latest(self, tenant_id: str, data_source_id: str) -> SchemaExplorerSnapshot:
        if self._repository.get_data_source(tenant_id, data_source_id) is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        version = self._repository.get_latest_catalog_version(tenant_id, data_source_id)
        if version is None:
            raise CatalogNotIngestedError("DataSource has no catalog version")

        schema_objects = self._repository.list_schema_objects(tenant_id, version.id)
        columns_by_object: dict[str, list[ColumnDefinition]] = {}
        for column in self._repository.list_columns_for_catalog_version(
            tenant_id,
            version.id,
        ):
            columns_by_object.setdefault(column.schema_object_id, []).append(column)
        semantics = {
            resolution.object_ref: resolution
            for resolution in self._repository.list_semantic_resolutions(
                tenant_id,
                data_source_id,
            )
        }
        object_refs = {
            schema_object.id: schema_object.reference for schema_object in schema_objects
        }
        objects = tuple(
            ObjectExplorerEntry(
                id=schema_object.id,
                reference=schema_object.reference,
                schema_name=schema_object.schema_name,
                name=schema_object.name,
                kind=schema_object.kind,
                definition_sql=schema_object.definition_sql,
                semantics=_semantic_entry(semantics.get(schema_object.reference)),
                columns=tuple(
                    ColumnExplorerEntry(
                        name=column.name,
                        physical_type=column.physical_type,
                        ordinal=column.ordinal,
                        nullable=column.nullable,
                        classification=column.classification,
                        default_expression=column.default_expression,
                        is_primary_key=column.is_primary_key,
                        semantics=_semantic_entry(
                            semantics.get(
                                f"{schema_object.reference}.{column.name}",
                            )
                        ),
                    )
                    for column in columns_by_object.get(schema_object.id, ())
                ),
            )
            for schema_object in schema_objects
        )
        relationships = tuple(
            RelationshipExplorerEntry(
                name=relationship.name,
                source_object_ref=object_refs[relationship.source_object_id],
                target_object_ref=object_refs[relationship.target_object_id],
                source_columns=relationship.source_columns,
                target_columns=relationship.target_columns,
                status=relationship.status,
                confidence=relationship.confidence,
            )
            for relationship in self._repository.list_relationships(tenant_id, version.id)
        )
        return SchemaExplorerSnapshot(
            data_source_id=data_source_id,
            catalog_version_id=version.id,
            catalog_version=version.version,
            created_at=version.created_at,
            objects=objects,
            relationships=relationships,
        )


def _semantic_entry(resolution: SemanticResolution | None) -> SemanticExplorerEntry | None:
    if resolution is None:
        return None
    return SemanticExplorerEntry(
        description=resolution.description,
        status=resolution.status,
        confidence=resolution.confidence,
        updated_at=resolution.updated_at,
    )
