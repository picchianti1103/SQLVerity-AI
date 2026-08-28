from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
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
    PostgreSQLConnectionSecret,
    SecretResolver,
)


class Cursor(Protocol):
    description: Any

    def execute(self, query: str) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __enter__(self) -> Cursor: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...


ConnectFactory = Callable[..., AbstractContextManager[Connection]]


_OBJECTS_SQL = """
SELECT
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    relation.relkind AS relation_kind,
    CASE
        WHEN relation.relkind IN ('v', 'm') THEN pg_catalog.pg_get_viewdef(relation.oid, true)
        ELSE NULL
    END AS definition_sql,
    pg_catalog.obj_description(relation.oid, 'pg_class') AS comment
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
  AND namespace.nspname NOT LIKE 'pg_toast%'
ORDER BY namespace.nspname, relation.relname
"""

_COLUMNS_SQL = """
SELECT
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    attribute.attname AS column_name,
    pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS physical_type,
    attribute.attnum AS ordinal,
    NOT attribute.attnotnull AS nullable,
    pg_catalog.pg_get_expr(default_value.adbin, default_value.adrelid) AS default_expression,
    pg_catalog.col_description(relation.oid, attribute.attnum) AS comment
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.oid
LEFT JOIN pg_catalog.pg_attrdef AS default_value
    ON default_value.adrelid = relation.oid
   AND default_value.adnum = attribute.attnum
WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
  AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
  AND namespace.nspname NOT LIKE 'pg_toast%'
ORDER BY namespace.nspname, relation.relname, attribute.attnum
"""

_PRIMARY_KEYS_SQL = """
SELECT
    namespace.nspname AS schema_name,
    relation.relname AS object_name,
    constraint_row.conname AS constraint_name,
    array_agg(attribute.attname ORDER BY key_column.ordinality) AS column_names
FROM pg_catalog.pg_constraint AS constraint_row
JOIN pg_catalog.pg_class AS relation ON relation.oid = constraint_row.conrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
CROSS JOIN LATERAL unnest(constraint_row.conkey)
    WITH ORDINALITY AS key_column(attribute_number, ordinality)
JOIN pg_catalog.pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attnum = key_column.attribute_number
WHERE constraint_row.contype = 'p'
  AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY namespace.nspname, relation.relname, constraint_row.conname
ORDER BY namespace.nspname, relation.relname, constraint_row.conname
"""

_FOREIGN_KEYS_SQL = """
SELECT
    source_namespace.nspname AS source_schema,
    source_relation.relname AS source_object,
    target_namespace.nspname AS target_schema,
    target_relation.relname AS target_object,
    constraint_row.conname AS constraint_name,
    array_agg(source_attribute.attname ORDER BY source_key.ordinality) AS source_columns,
    array_agg(target_attribute.attname ORDER BY source_key.ordinality) AS target_columns
FROM pg_catalog.pg_constraint AS constraint_row
JOIN pg_catalog.pg_class AS source_relation ON source_relation.oid = constraint_row.conrelid
JOIN pg_catalog.pg_namespace AS source_namespace
    ON source_namespace.oid = source_relation.relnamespace
JOIN pg_catalog.pg_class AS target_relation ON target_relation.oid = constraint_row.confrelid
JOIN pg_catalog.pg_namespace AS target_namespace
    ON target_namespace.oid = target_relation.relnamespace
CROSS JOIN LATERAL unnest(constraint_row.conkey)
    WITH ORDINALITY AS source_key(attribute_number, ordinality)
JOIN LATERAL unnest(constraint_row.confkey)
    WITH ORDINALITY AS target_key(attribute_number, ordinality)
    ON target_key.ordinality = source_key.ordinality
JOIN pg_catalog.pg_attribute AS source_attribute
    ON source_attribute.attrelid = source_relation.oid
   AND source_attribute.attnum = source_key.attribute_number
JOIN pg_catalog.pg_attribute AS target_attribute
    ON target_attribute.attrelid = target_relation.oid
   AND target_attribute.attnum = target_key.attribute_number
WHERE constraint_row.contype = 'f'
  AND source_namespace.nspname NOT IN ('pg_catalog', 'information_schema')
  AND target_namespace.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY
    source_namespace.nspname,
    source_relation.relname,
    target_namespace.nspname,
    target_relation.relname,
    constraint_row.conname
ORDER BY source_namespace.nspname, source_relation.relname, constraint_row.conname
"""


class PostgreSQLConnector:
    """Read-only PostgreSQL metadata connector for PostgreSQL 16 and newer."""

    def __init__(
        self,
        secret_resolver: SecretResolver,
        connect_factory: ConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory

    def capabilities(self, data_source: DataSource) -> frozenset[DataSourceCapability]:
        if data_source.dialect.casefold() not in {"postgres", "postgresql"}:
            return frozenset()
        return frozenset({DataSourceCapability.INTROSPECT})

    def introspect(self, data_source: DataSource) -> DataSourceSnapshot:
        self._validate_data_source(data_source)
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")
        secret = self._secret_resolver.resolve_postgresql(secret_ref)

        try:
            with self._connect(secret) as connection, connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                object_rows = _fetch_dicts(cursor, _OBJECTS_SQL)
                column_rows = _fetch_dicts(cursor, _COLUMNS_SQL)
                primary_key_rows = _fetch_dicts(cursor, _PRIMARY_KEYS_SQL)
                relationship_rows = _fetch_dicts(cursor, _FOREIGN_KEYS_SQL)
        except ConnectorUnavailableError:
            raise
        except Exception:
            raise ConnectorUnavailableError("PostgreSQL introspection failed") from None

        primary_keys = {
            _object_ref(row["schema_name"], row["object_name"]): frozenset(row["column_names"])
            for row in primary_key_rows
        }
        columns_by_object: dict[str, list[ColumnSnapshot]] = {}
        for row in column_rows:
            reference = _object_ref(row["schema_name"], row["object_name"])
            columns_by_object.setdefault(reference, []).append(
                ColumnSnapshot(
                    name=row["column_name"],
                    physical_type=row["physical_type"],
                    ordinal=int(row["ordinal"]),
                    nullable=bool(row["nullable"]),
                    default_expression=row["default_expression"],
                    is_primary_key=row["column_name"] in primary_keys.get(reference, frozenset()),
                    comment=row["comment"],
                )
            )

        objects = tuple(
            SchemaObjectSnapshot(
                schema_name=row["schema_name"],
                name=row["object_name"],
                kind=_object_kind(row["relation_kind"]),
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
        relationships = tuple(
            RelationshipSnapshot(
                name=row["constraint_name"],
                source_object_ref=_object_ref(row["source_schema"], row["source_object"]),
                target_object_ref=_object_ref(row["target_schema"], row["target_object"]),
                source_columns=tuple(row["source_columns"]),
                target_columns=tuple(row["target_columns"]),
            )
            for row in relationship_rows
        )
        return DataSourceSnapshot(
            data_source_id=data_source.id,
            dialect="postgresql",
            objects=objects,
            relationships=relationships,
        )

    def _validate_data_source(self, data_source: DataSource) -> None:
        if data_source.dialect.casefold() not in {"postgres", "postgresql"}:
            raise ConnectorConfigurationError("PostgreSQLConnector requires PostgreSQL dialect")
        if DataSourceCapability.INTROSPECT not in data_source.capabilities:
            raise ConnectorConfigurationError("DataSource does not allow introspection")
        if data_source.connection_secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")

    def _connect(self, secret: PostgreSQLConnectionSecret) -> AbstractContextManager[Connection]:
        if self._connect_factory is not None:
            return self._connect_factory(**secret.as_connect_kwargs())
        try:
            import psycopg
        except ImportError:
            raise ConnectorUnavailableError("Install the 'psycopg' project dependency") from None
        connection = psycopg.connect(
            host=secret.host,
            port=secret.port,
            dbname=secret.database,
            user=secret.username,
            password=secret.password,
            sslmode=secret.sslmode,
            connect_timeout=secret.connect_timeout_seconds,
            application_name="sqlverity-introspection",
        )
        return cast(AbstractContextManager[Connection], connection)


def _fetch_dicts(cursor: Cursor, query: str) -> tuple[dict[str, Any], ...]:
    cursor.execute(query)
    names = tuple(
        description.name if hasattr(description, "name") else description[0]
        for description in cursor.description
    )
    return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())


def _object_ref(schema_name: str, object_name: str) -> str:
    return f"{schema_name}.{object_name}"


def _object_kind(relation_kind: str) -> ObjectKind:
    if relation_kind in {"v", "m"}:
        return ObjectKind.VIEW
    if relation_kind in {"r", "p", "f"}:
        return ObjectKind.TABLE
    raise ValueError(f"Unsupported PostgreSQL relation kind: {relation_kind}")
