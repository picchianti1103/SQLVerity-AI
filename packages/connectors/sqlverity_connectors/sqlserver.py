from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from importlib import import_module
from typing import Any, Protocol, cast

from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    RelationshipSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import DataSource, DataSourceCapability, ObjectKind

from .connection import (
    ConnectorConfigurationError,
    ConnectorUnavailableError,
    SQLServerConnectionSecret,
    SQLServerSecretResolver,
)


class SQLServerCursor(Protocol):
    description: Any

    def execute(self, query: str, parameters: object = None) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __enter__(self) -> SQLServerCursor: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class SQLServerConnection(Protocol):
    def cursor(self) -> SQLServerCursor: ...

    def set_attr(self, attribute: int, value: object) -> None: ...


class SQLServerReadOnlyAttributeConnection(Protocol):
    def set_attr(self, attribute: int, value: object) -> None: ...


SQLServerConnectFactory = Callable[..., AbstractContextManager[SQLServerConnection]]


_OBJECTS_SQL = """
SELECT
    schema_row.name AS schema_name,
    object_row.name AS object_name,
    object_row.type AS object_type,
    OBJECT_DEFINITION(object_row.object_id) AS definition_sql,
    CAST(property_row.value AS nvarchar(max)) AS object_comment
FROM sys.objects object_row
JOIN sys.schemas schema_row ON schema_row.schema_id = object_row.schema_id
LEFT JOIN sys.extended_properties property_row
  ON property_row.major_id = object_row.object_id
 AND property_row.minor_id = 0
 AND property_row.name = N'MS_Description'
WHERE object_row.type IN ('U', 'V')
  AND object_row.is_ms_shipped = 0
ORDER BY schema_row.name, object_row.name
"""

_COLUMNS_SQL = """
SELECT
    schema_row.name AS schema_name,
    object_row.name AS object_name,
    column_row.name AS column_name,
    type_row.name AS data_type,
    column_row.max_length AS max_length,
    column_row.precision AS data_precision,
    column_row.scale AS data_scale,
    column_row.column_id AS ordinal,
    column_row.is_nullable AS is_nullable,
    default_row.definition AS default_expression,
    CAST(property_row.value AS nvarchar(max)) AS column_comment
FROM sys.objects object_row
JOIN sys.schemas schema_row ON schema_row.schema_id = object_row.schema_id
JOIN sys.columns column_row ON column_row.object_id = object_row.object_id
JOIN sys.types type_row ON type_row.user_type_id = column_row.user_type_id
LEFT JOIN sys.default_constraints default_row
  ON default_row.object_id = column_row.default_object_id
LEFT JOIN sys.extended_properties property_row
  ON property_row.major_id = object_row.object_id
 AND property_row.minor_id = column_row.column_id
 AND property_row.name = N'MS_Description'
WHERE object_row.type IN ('U', 'V')
  AND object_row.is_ms_shipped = 0
ORDER BY schema_row.name, object_row.name, column_row.column_id
"""

_PRIMARY_KEYS_SQL = """
SELECT
    schema_row.name AS schema_name,
    object_row.name AS object_name,
    column_row.name AS column_name,
    index_column.key_ordinal AS ordinal
FROM sys.indexes index_row
JOIN sys.objects object_row ON object_row.object_id = index_row.object_id
JOIN sys.schemas schema_row ON schema_row.schema_id = object_row.schema_id
JOIN sys.index_columns index_column
  ON index_column.object_id = index_row.object_id
 AND index_column.index_id = index_row.index_id
JOIN sys.columns column_row
  ON column_row.object_id = index_column.object_id
 AND column_row.column_id = index_column.column_id
WHERE index_row.is_primary_key = 1
ORDER BY schema_row.name, object_row.name, index_column.key_ordinal
"""

_FOREIGN_KEYS_SQL = """
SELECT
    source_schema.name AS source_schema,
    source_object.name AS source_object,
    target_schema.name AS target_schema,
    target_object.name AS target_object,
    foreign_key.name AS constraint_name,
    source_column.name AS source_column,
    target_column.name AS target_column,
    key_column.constraint_column_id AS ordinal
FROM sys.foreign_keys foreign_key
JOIN sys.foreign_key_columns key_column
  ON key_column.constraint_object_id = foreign_key.object_id
JOIN sys.objects source_object ON source_object.object_id = foreign_key.parent_object_id
JOIN sys.schemas source_schema ON source_schema.schema_id = source_object.schema_id
JOIN sys.columns source_column
  ON source_column.object_id = key_column.parent_object_id
 AND source_column.column_id = key_column.parent_column_id
JOIN sys.objects target_object ON target_object.object_id = foreign_key.referenced_object_id
JOIN sys.schemas target_schema ON target_schema.schema_id = target_object.schema_id
JOIN sys.columns target_column
  ON target_column.object_id = key_column.referenced_object_id
 AND target_column.column_id = key_column.referenced_column_id
ORDER BY source_schema.name, source_object.name, foreign_key.name, key_column.constraint_column_id
"""


class SQLServerConnector:
    """Read-only current-database catalog connector for Microsoft SQL Server."""

    def __init__(
        self,
        secret_resolver: SQLServerSecretResolver,
        connect_factory: SQLServerConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory

    def capabilities(self, data_source: DataSource) -> frozenset[DataSourceCapability]:
        if data_source.dialect.casefold() not in {"mssql", "sqlserver", "tsql"}:
            return frozenset()
        return frozenset({DataSourceCapability.INTROSPECT})

    def introspect(self, data_source: DataSource) -> DataSourceSnapshot:
        self._validate_data_source(data_source)
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")
        secret = self._secret_resolver.resolve_sqlserver(secret_ref)
        try:
            with self._connect(secret) as connection:
                _set_read_only(connection)
                with connection.cursor() as cursor:
                    object_rows = _fetch_dicts(cursor, _OBJECTS_SQL)
                    column_rows = _fetch_dicts(cursor, _COLUMNS_SQL)
                    primary_key_rows = _fetch_dicts(cursor, _PRIMARY_KEYS_SQL)
                    relationship_rows = _fetch_dicts(cursor, _FOREIGN_KEYS_SQL)
        except ConnectorUnavailableError:
            raise
        except Exception:
            raise ConnectorUnavailableError("SQL Server introspection failed") from None

        primary_keys: dict[str, set[str]] = {
            _object_ref(row["schema_name"], row["object_name"]): set()
            for row in primary_key_rows
        }
        for row in primary_key_rows:
            primary_keys[_object_ref(row["schema_name"], row["object_name"])].add(
                str(row["column_name"]).casefold()
            )
        columns_by_object: dict[str, list[ColumnSnapshot]] = {}
        for row in column_rows:
            reference = _object_ref(row["schema_name"], row["object_name"])
            columns_by_object.setdefault(reference, []).append(
                ColumnSnapshot(
                    name=str(row["column_name"]),
                    physical_type=_physical_type(row),
                    ordinal=int(row["ordinal"]),
                    nullable=bool(row["is_nullable"]),
                    default_expression=_optional_text(row["default_expression"]),
                    is_primary_key=(
                        str(row["column_name"]).casefold()
                        in primary_keys.get(reference, set())
                    ),
                    comment=_optional_text(row["column_comment"]),
                )
            )
        objects = tuple(
            SchemaObjectSnapshot(
                schema_name=str(row["schema_name"]),
                name=str(row["object_name"]),
                kind=(
                    ObjectKind.VIEW
                    if str(row["object_type"]).casefold() == "v"
                    else ObjectKind.TABLE
                ),
                columns=tuple(
                    columns_by_object.get(
                        _object_ref(row["schema_name"], row["object_name"]),
                        (),
                    )
                ),
                definition_sql=_optional_text(row["definition_sql"]),
                comment=_optional_text(row["object_comment"]),
            )
            for row in object_rows
        )
        return DataSourceSnapshot(
            data_source_id=data_source.id,
            dialect="sqlserver",
            objects=objects,
            relationships=_relationships(relationship_rows),
        )

    def _validate_data_source(self, data_source: DataSource) -> None:
        if data_source.dialect.casefold() not in {"mssql", "sqlserver", "tsql"}:
            raise ConnectorConfigurationError(
                "SQLServerConnector requires SQL Server dialect"
            )
        if DataSourceCapability.INTROSPECT not in data_source.capabilities:
            raise ConnectorConfigurationError("DataSource does not allow introspection")
        if data_source.connection_secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")

    def _connect(
        self, secret: SQLServerConnectionSecret
    ) -> AbstractContextManager[SQLServerConnection]:
        if self._connect_factory is not None:
            return self._connect_factory(**secret.as_connect_kwargs())
        try:
            module = import_module("mssql_python")
            connection = module.connect(**secret.as_connect_kwargs())
        except ImportError:
            raise ConnectorUnavailableError(
                "Install the 'mssql-python' project dependency"
            ) from None
        except Exception:
            raise ConnectorUnavailableError("SQL Server connection failed") from None
        return cast(AbstractContextManager[SQLServerConnection], connection)


def _set_read_only(connection: SQLServerReadOnlyAttributeConnection) -> None:
    try:
        module = import_module("mssql_python")
        access_mode = int(module.SQL_ATTR_ACCESS_MODE)
        read_only = module.SQL_MODE_READ_ONLY
    except (AttributeError, ImportError):
        # ODBC constants; the fallback keeps injected test doubles independent
        # from the optional native driver while production uses exported values.
        access_mode = 101
        read_only = 1
    connection.set_attr(access_mode, read_only)


def _fetch_dicts(cursor: SQLServerCursor, query: str) -> tuple[dict[str, Any], ...]:
    cursor.execute(query)
    names = tuple(
        str(item.name if hasattr(item, "name") else item[0]).casefold()
        for item in cursor.description
    )
    return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())


def _physical_type(row: dict[str, Any]) -> str:
    data_type = str(row["data_type"])
    if data_type in {"char", "nchar", "nvarchar", "varbinary", "varchar", "binary"}:
        raw_length = int(row["max_length"])
        if raw_length == -1:
            length = "max"
        else:
            length_value = raw_length // 2 if data_type in {"nchar", "nvarchar"} else raw_length
            length = str(length_value)
        return f"{data_type}({length})"
    if data_type in {"decimal", "numeric"}:
        return f"{data_type}({int(row['data_precision'])},{int(row['data_scale'])})"
    if data_type in {"datetime2", "datetimeoffset", "time"}:
        return f"{data_type}({int(row['data_scale'])})"
    return data_type


def _relationships(rows: tuple[dict[str, Any], ...]) -> tuple[RelationshipSnapshot, ...]:
    grouped: dict[tuple[str, str, str, str, str], tuple[list[str], list[str]]] = {}
    for row in rows:
        key = (
            str(row["source_schema"]),
            str(row["source_object"]),
            str(row["target_schema"]),
            str(row["target_object"]),
            str(row["constraint_name"]),
        )
        source_columns, target_columns = grouped.setdefault(key, ([], []))
        source_columns.append(str(row["source_column"]))
        target_columns.append(str(row["target_column"]))
    return tuple(
        RelationshipSnapshot(
            name=constraint_name,
            source_object_ref=f"{source_schema}.{source_object}",
            target_object_ref=f"{target_schema}.{target_object}",
            source_columns=tuple(source_columns),
            target_columns=tuple(target_columns),
        )
        for (
            source_schema,
            source_object,
            target_schema,
            target_object,
            constraint_name,
        ), (source_columns, target_columns) in grouped.items()
    )


def _object_ref(schema_name: object, object_name: object) -> str:
    return f"{schema_name}.{object_name}".casefold()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
