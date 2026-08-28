from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from packages.connectors.sqlverity_connectors.ddl import (
    DDLParseError,
    DialectDDLParser,
    MariaDBDDLParser,
    MySQLDDLParser,
    OracleDDLParser,
    PostgreSQLDDLParser,
    SQLServerDDLParser,
)
from packages.domain.sqlverity_domain.contracts import DataSourceSnapshot
from packages.domain.sqlverity_domain.models import DataSource, DataSourceType

from .ingestion import CatalogIngestionService, DataSourceNotFoundError, IngestionReport
from .repository import SQLiteCatalogRepository


class OfflineImportModeError(ValueError):
    pass


class OfflineSchemaImportService:
    def __init__(
        self,
        repository: SQLiteCatalogRepository,
        ingestion: CatalogIngestionService,
        ddl_parsers: Mapping[str, DialectDDLParser] | None = None,
    ) -> None:
        self._repository = repository
        self._ingestion = ingestion
        self._ddl_parsers = {
            key.casefold(): value
            for key, value in (
                ddl_parsers
                or {
                    "postgres": PostgreSQLDDLParser(),
                    "postgresql": PostgreSQLDDLParser(),
                    "mysql": MySQLDDLParser(),
                    "mariadb": MariaDBDDLParser(),
                    "oracle": OracleDDLParser(),
                    "mssql": SQLServerDDLParser(),
                    "sqlserver": SQLServerDDLParser(),
                    "tsql": SQLServerDDLParser(),
                }
            ).items()
        }

    def import_manual(
        self,
        tenant_id: str,
        data_source_id: str,
        snapshot: DataSourceSnapshot,
    ) -> IngestionReport:
        data_source = self._require_source_type(
            tenant_id,
            data_source_id,
            {DataSourceType.MANUAL_SCHEMA, DataSourceType.HYBRID},
        )
        return self._ingestion.ingest_snapshot(
            tenant_id,
            data_source_id,
            replace(snapshot, dialect=data_source.dialect),
        )

    def import_ddl(
        self,
        tenant_id: str,
        data_source_id: str,
        ddl: str,
        *,
        default_schema: str | None = None,
    ) -> IngestionReport:
        data_source = self._require_source_type(
            tenant_id,
            data_source_id,
            {DataSourceType.DDL_IMPORT, DataSourceType.HYBRID},
        )
        parser = self._ddl_parsers.get(data_source.dialect.casefold())
        if parser is None:
            raise DDLParseError(f"No DDL parser registered for {data_source.dialect}")
        if default_schema is None:
            if data_source.dialect.casefold() in {"postgres", "postgresql"}:
                default_schema = "public"
            elif data_source.dialect.casefold() in {"mssql", "sqlserver", "tsql"}:
                default_schema = "dbo"
            else:
                raise DDLParseError(
                    "default_schema is required for MySQL, MariaDB, and Oracle DDL imports"
                )
        snapshot = parser.parse(
            data_source_id=data_source_id,
            ddl=ddl,
            default_schema=default_schema,
        )
        return self._ingestion.ingest_snapshot(tenant_id, data_source_id, snapshot)

    def _require_source_type(
        self,
        tenant_id: str,
        data_source_id: str,
        allowed_types: set[DataSourceType],
    ) -> DataSource:
        data_source = self._repository.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError("DataSource does not exist in this tenant")
        if data_source.source_type not in allowed_types:
            allowed = ", ".join(sorted(source_type.value for source_type in allowed_types))
            raise OfflineImportModeError(
                f"DataSource type {data_source.source_type.value} does not allow this import; "
                f"expected one of: {allowed}"
            )
        return data_source
