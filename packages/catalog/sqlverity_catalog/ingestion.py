from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from packages.domain.sqlverity_domain.contracts import Connector, DataSourceSnapshot
from packages.domain.sqlverity_domain.models import EpistemicStatus, SemanticDefinition

from .repository import SQLiteCatalogRepository


class CatalogIngestionError(RuntimeError):
    pass


class DataSourceNotFoundError(CatalogIngestionError):
    pass


class ConnectorNotFoundError(CatalogIngestionError):
    pass


class InvalidSnapshotError(CatalogIngestionError):
    pass


@dataclass(frozen=True, slots=True)
class IngestionReport:
    catalog_version_id: str
    catalog_version: int
    object_count: int
    column_count: int
    relationship_count: int
    imported_description_count: int


@dataclass(frozen=True, slots=True)
class ConnectionTestReport:
    data_source_id: str
    dialect: str
    object_count: int
    relationship_count: int
    capabilities: tuple[str, ...]


class CatalogIngestionService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        connectors: Mapping[str, Connector],
    ) -> None:
        self._repository = repository
        self._connectors = {
            dialect.casefold(): connector for dialect, connector in connectors.items()
        }

    def test_connection(self, tenant_id: str, data_source_id: str) -> ConnectionTestReport:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        connector = self._connectors.get(data_source.dialect.casefold())
        if connector is None:
            raise ConnectorNotFoundError(f"No connector registered for {data_source.dialect}")

        try:
            snapshot = connector.introspect(data_source)
            self._validate_snapshot(snapshot, data_source_id, data_source.dialect)
            report = ConnectionTestReport(
                data_source_id=data_source_id,
                dialect=data_source.dialect,
                object_count=len(snapshot.objects),
                relationship_count=len(snapshot.relationships),
                capabilities=tuple(
                    sorted(capability.value for capability in connector.capabilities(data_source))
                ),
            )
        except Exception as error:
            self._repository.record_data_source_activity(
                tenant_id,
                data_source_id,
                "data_source.connection_test_failed",
                {"error_type": type(error).__name__},
            )
            raise
        self._repository.record_data_source_activity(
            tenant_id,
            data_source_id,
            "data_source.connection_test_succeeded",
            {
                "dialect": report.dialect,
                "object_count": report.object_count,
                "relationship_count": report.relationship_count,
                "capabilities": report.capabilities,
            },
        )
        return report

    def ingest(self, tenant_id: str, data_source_id: str) -> IngestionReport:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        connector = self._connectors.get(data_source.dialect.casefold())
        if connector is None:
            raise ConnectorNotFoundError(f"No connector registered for {data_source.dialect}")

        snapshot = connector.introspect(data_source)
        return self.ingest_snapshot(tenant_id, data_source_id, snapshot)

    def ingest_snapshot(
        self,
        tenant_id: str,
        data_source_id: str,
        snapshot: DataSourceSnapshot,
        *,
        semantic_source: str = "database_comment",
    ) -> IngestionReport:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        self._validate_snapshot(snapshot, data_source_id, data_source.dialect)

        version = self._repository.create_catalog_version(tenant_id, data_source_id)
        object_ids: dict[str, str] = {}
        column_count = 0
        description_count = 0

        for object_snapshot in snapshot.objects:
            schema_object = self._repository.create_schema_object(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                catalog_version_id=version.id,
                schema_name=object_snapshot.schema_name,
                name=object_snapshot.name,
                kind=object_snapshot.kind,
                definition_sql=object_snapshot.definition_sql,
            )
            object_ids[object_snapshot.reference] = schema_object.id
            if object_snapshot.comment:
                self._import_description(
                    tenant_id,
                    version.id,
                    object_snapshot.reference,
                    object_snapshot.comment,
                    semantic_source,
                )
                description_count += 1

            for column_snapshot in object_snapshot.columns:
                self._repository.create_column(
                    tenant_id=tenant_id,
                    schema_object_id=schema_object.id,
                    name=column_snapshot.name,
                    physical_type=column_snapshot.physical_type,
                    ordinal=column_snapshot.ordinal,
                    nullable=column_snapshot.nullable,
                    classification=column_snapshot.classification,
                    default_expression=column_snapshot.default_expression,
                    is_primary_key=column_snapshot.is_primary_key,
                )
                column_count += 1
                if column_snapshot.comment:
                    self._import_description(
                        tenant_id,
                        version.id,
                        f"{object_snapshot.reference}.{column_snapshot.name}",
                        column_snapshot.comment,
                        semantic_source,
                    )
                    description_count += 1

        for relationship in snapshot.relationships:
            self._repository.create_relationship(
                tenant_id=tenant_id,
                catalog_version_id=version.id,
                source_object_id=object_ids[relationship.source_object_ref],
                target_object_id=object_ids[relationship.target_object_ref],
                name=relationship.name,
                source_columns=relationship.source_columns,
                target_columns=relationship.target_columns,
            )

        return IngestionReport(
            catalog_version_id=version.id,
            catalog_version=version.version,
            object_count=len(snapshot.objects),
            column_count=column_count,
            relationship_count=len(snapshot.relationships),
            imported_description_count=description_count,
        )

    def _import_description(
        self,
        tenant_id: str,
        catalog_version_id: str,
        object_ref: str,
        description: str,
        source: str,
    ) -> None:
        self._repository.propose_semantic_definition(
            SemanticDefinition(
                tenant_id=tenant_id,
                catalog_version_id=catalog_version_id,
                object_ref=object_ref,
                description=description,
                status=EpistemicStatus.IMPORTED,
                source=source,
                confidence=1.0,
            )
        )

    @staticmethod
    def _validate_snapshot(
        snapshot: DataSourceSnapshot,
        expected_data_source_id: str,
        expected_dialect: str,
    ) -> None:
        if snapshot.data_source_id != expected_data_source_id:
            raise InvalidSnapshotError("Connector returned a snapshot for another DataSource")
        if snapshot.dialect.casefold() != expected_dialect.casefold():
            raise InvalidSnapshotError("Connector returned a snapshot for another dialect")
        references = [schema_object.reference for schema_object in snapshot.objects]
        if len(references) != len(set(references)):
            raise InvalidSnapshotError("Connector returned duplicate schema objects")

        objects_by_ref = {
            schema_object.reference: schema_object for schema_object in snapshot.objects
        }
        for schema_object in snapshot.objects:
            column_names = [column.name for column in schema_object.columns]
            column_ordinals = [column.ordinal for column in schema_object.columns]
            if len(column_names) != len(set(column_names)):
                raise InvalidSnapshotError(
                    f"Connector returned duplicate columns for {schema_object.reference}"
                )
            if len(column_ordinals) != len(set(column_ordinals)):
                raise InvalidSnapshotError(
                    f"Connector returned duplicate column ordinals for {schema_object.reference}"
                )

        relationship_names = [relationship.name for relationship in snapshot.relationships]
        if len(relationship_names) != len(set(relationship_names)):
            raise InvalidSnapshotError("Connector returned duplicate relationship names")
        for relationship in snapshot.relationships:
            if (
                relationship.source_object_ref not in objects_by_ref
                or relationship.target_object_ref not in objects_by_ref
            ):
                raise InvalidSnapshotError(
                    f"Relationship {relationship.name} references an unknown schema object"
                )
            source = objects_by_ref[relationship.source_object_ref]
            target = objects_by_ref[relationship.target_object_ref]
            source_columns = {column.name for column in source.columns}
            target_columns = {column.name for column in target.columns}
            if not set(relationship.source_columns).issubset(source_columns):
                raise InvalidSnapshotError(
                    f"Relationship {relationship.name} has unknown source columns"
                )
            if not set(relationship.target_columns).issubset(target_columns):
                raise InvalidSnapshotError(
                    f"Relationship {relationship.name} has unknown target columns"
                )
