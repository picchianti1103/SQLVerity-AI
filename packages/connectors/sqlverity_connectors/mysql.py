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
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    ObjectKind,
)

from .connection import (
    ConnectorConfigurationError,
    ConnectorUnavailableError,
    MySQLConnectionSecret,
    MySQLSecretResolver,
)


class MySQLCursor(Protocol):
    description: Any

    def execute(self, query: str, parameters: object = None) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __enter__(self) -> MySQLCursor: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class MySQLConnection(Protocol):
    def cursor(self, **kwargs: Any) -> MySQLCursor: ...


MySQLConnectFactory = Callable[..., AbstractContextManager[MySQLConnection]]


_OBJECTS_SQL = """
SELECT
    table_row.TABLE_SCHEMA AS schema_name,
    table_row.TABLE_NAME AS object_name,
    table_row.TABLE_TYPE AS table_type,
    view_row.VIEW_DEFINITION AS definition_sql,
    NULLIF(table_row.TABLE_COMMENT, '') AS comment
FROM information_schema.TABLES AS table_row
LEFT JOIN information_schema.VIEWS AS view_row
  ON view_row.TABLE_SCHEMA = table_row.TABLE_SCHEMA
 AND view_row.TABLE_NAME = table_row.TABLE_NAME
WHERE table_row.TABLE_SCHEMA = DATABASE()
  AND table_row.TABLE_TYPE IN ('BASE TABLE', 'VIEW', 'SYSTEM VERSIONED')
ORDER BY table_row.TABLE_NAME
"""

_COLUMNS_SQL = """
SELECT
    column_row.TABLE_SCHEMA AS schema_name,
    column_row.TABLE_NAME AS object_name,
    column_row.COLUMN_NAME AS column_name,
    column_row.COLUMN_TYPE AS physical_type,
    column_row.ORDINAL_POSITION AS ordinal,
    column_row.IS_NULLABLE AS is_nullable,
    column_row.COLUMN_DEFAULT AS default_expression,
    NULLIF(column_row.COLUMN_COMMENT, '') AS comment
FROM information_schema.COLUMNS AS column_row
WHERE column_row.TABLE_SCHEMA = DATABASE()
ORDER BY column_row.TABLE_NAME, column_row.ORDINAL_POSITION
"""

_PRIMARY_KEYS_SQL = """
SELECT
    key_row.TABLE_SCHEMA AS schema_name,
    key_row.TABLE_NAME AS object_name,
    key_row.COLUMN_NAME AS column_name,
    key_row.ORDINAL_POSITION AS ordinal
FROM information_schema.KEY_COLUMN_USAGE AS key_row
WHERE key_row.TABLE_SCHEMA = DATABASE()
  AND key_row.CONSTRAINT_NAME = 'PRIMARY'
ORDER BY key_row.TABLE_NAME, key_row.ORDINAL_POSITION
"""

_FOREIGN_KEYS_SQL = """
SELECT
    key_row.TABLE_SCHEMA AS source_schema,
    key_row.TABLE_NAME AS source_object,
    key_row.REFERENCED_TABLE_SCHEMA AS target_schema,
    key_row.REFERENCED_TABLE_NAME AS target_object,
    key_row.CONSTRAINT_NAME AS constraint_name,
    key_row.COLUMN_NAME AS source_column,
    key_row.REFERENCED_COLUMN_NAME AS target_column,
    key_row.ORDINAL_POSITION AS ordinal
FROM information_schema.KEY_COLUMN_USAGE AS key_row
WHERE key_row.TABLE_SCHEMA = DATABASE()
  AND key_row.REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY key_row.TABLE_NAME, key_row.CONSTRAINT_NAME, key_row.ORDINAL_POSITION
"""


class MySQLConnector:
    """Read-only information_schema connector shared by MySQL and MariaDB."""

    def __init__(
        self,
        secret_resolver: MySQLSecretResolver,
        connect_factory: MySQLConnectFactory | None = None,
        *,
        dialect: str = "mysql",
    ) -> None:
        normalized = dialect.casefold()
        if normalized not in {"mysql", "mariadb"}:
            raise ValueError("MySQLConnector dialect must be mysql or mariadb")
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory
        self._dialect = normalized

    def capabilities(self, data_source: DataSource) -> frozenset[DataSourceCapability]:
        if data_source.dialect.casefold() != self._dialect:
            return frozenset()
        return frozenset({DataSourceCapability.INTROSPECT})

    def introspect(self, data_source: DataSource) -> DataSourceSnapshot:
        self._validate_data_source(data_source)
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")
        secret = self._secret_resolver.resolve_mysql(secret_ref)
        try:
            with self._connect(secret) as connection, connection.cursor(buffered=True) as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                object_rows = _fetch_dicts(cursor, _OBJECTS_SQL)
                column_rows = _fetch_dicts(cursor, _COLUMNS_SQL)
                primary_key_rows = _fetch_dicts(cursor, _PRIMARY_KEYS_SQL)
                relationship_rows = _fetch_dicts(cursor, _FOREIGN_KEYS_SQL)
        except ConnectorUnavailableError:
            raise
        except Exception:
            raise ConnectorUnavailableError(
                f"{self._dialect} introspection failed"
            ) from None

        primary_keys: dict[str, set[str]] = {
            _object_ref(row["schema_name"], row["object_name"],): set()
            for row in primary_key_rows
        }
        for row in primary_key_rows:
            primary_keys[_object_ref(row["schema_name"], row["object_name"])].add(
                row["column_name"]
            )
        columns_by_object: dict[str, list[ColumnSnapshot]] = {}
        for row in column_rows:
            reference = _object_ref(row["schema_name"], row["object_name"])
            columns_by_object.setdefault(reference, []).append(
                ColumnSnapshot(
                    name=row["column_name"],
                    physical_type=row["physical_type"],
                    ordinal=int(row["ordinal"]),
                    nullable=str(row["is_nullable"]).casefold() == "yes",
                    default_expression=row["default_expression"],
                    is_primary_key=row["column_name"] in primary_keys.get(reference, set()),
                    comment=row["comment"],
                )
            )
        objects = tuple(
            SchemaObjectSnapshot(
                schema_name=row["schema_name"],
                name=row["object_name"],
                kind=(
                    ObjectKind.VIEW
                    if str(row["table_type"]).casefold() == "view"
                    else ObjectKind.TABLE
                ),
                columns=tuple(
                    columns_by_object.get(
                        _object_ref(row["schema_name"], row["object_name"]),
                        (),
                    )
                ),
                definition_sql=row["definition_sql"],
                comment=row["comment"],
            )
            for row in object_rows
        )
        relationships = _relationships(relationship_rows)
        return DataSourceSnapshot(
            data_source_id=data_source.id,
            dialect=self._dialect,
            objects=objects,
            relationships=relationships,
        )

    def _validate_data_source(self, data_source: DataSource) -> None:
        if data_source.dialect.casefold() != self._dialect:
            raise ConnectorConfigurationError(
                f"MySQLConnector configured for {self._dialect} cannot handle "
                f"{data_source.dialect}"
            )
        if DataSourceCapability.INTROSPECT not in data_source.capabilities:
            raise ConnectorConfigurationError("DataSource does not allow introspection")
        if data_source.connection_secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")

    def _connect(
        self,
        secret: MySQLConnectionSecret,
    ) -> AbstractContextManager[MySQLConnection]:
        if self._connect_factory is not None:
            kwargs = (
                secret.as_mariadb_connect_kwargs()
                if self._dialect == "mariadb"
                else secret.as_connect_kwargs()
            )
            return self._connect_factory(**kwargs)
        if self._dialect == "mariadb":
            try:
                mariadb = import_module("mariadb")
            except ImportError:
                raise ConnectorUnavailableError(
                    "Install an organization-approved patched 'mariadb' driver separately"
                ) from None
            try:
                connection = mariadb.connect(**secret.as_mariadb_connect_kwargs())
            except Exception:
                raise ConnectorUnavailableError("mariadb connection failed") from None
            return cast(AbstractContextManager[MySQLConnection], connection)
        try:
            import mysql.connector
        except ImportError:
            raise ConnectorUnavailableError(
                "Install the 'mysql-connector-python' project dependency"
            ) from None
        try:
            connection = mysql.connector.connect(**secret.as_connect_kwargs())
        except Exception:
            raise ConnectorUnavailableError(f"{self._dialect} connection failed") from None
        return cast(AbstractContextManager[MySQLConnection], connection)


def _fetch_dicts(cursor: MySQLCursor, query: str) -> tuple[dict[str, Any], ...]:
    cursor.execute(query)
    names = tuple(
        description.name if hasattr(description, "name") else str(description[0])
        for description in cursor.description
    )
    return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())


def _relationships(rows: tuple[dict[str, Any], ...]) -> tuple[RelationshipSnapshot, ...]:
    grouped: dict[tuple[str, str, str, str, str], tuple[list[str], list[str]]] = {}
    for row in rows:
        key = (
            row["source_schema"],
            row["source_object"],
            row["target_schema"],
            row["target_object"],
            row["constraint_name"],
        )
        source_columns, target_columns = grouped.setdefault(key, ([], []))
        source_columns.append(row["source_column"])
        target_columns.append(row["target_column"])
    return tuple(
        RelationshipSnapshot(
            name=constraint_name,
            source_object_ref=_object_ref(source_schema, source_object),
            target_object_ref=_object_ref(target_schema, target_object),
            source_columns=tuple(columns[0]),
            target_columns=tuple(columns[1]),
        )
        for (
            source_schema,
            source_object,
            target_schema,
            target_object,
            constraint_name,
        ), columns in grouped.items()
    )


def _object_ref(schema_name: str, object_name: str) -> str:
    return f"{schema_name}.{object_name}"
