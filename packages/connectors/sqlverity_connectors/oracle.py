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
    OracleConnectionSecret,
    OracleSecretResolver,
)


class OracleCursor(Protocol):
    description: Any

    def execute(self, query: str, parameters: object = None) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __enter__(self) -> OracleCursor: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class OracleConnection(Protocol):
    def cursor(self) -> OracleCursor: ...


OracleConnectFactory = Callable[..., AbstractContextManager[OracleConnection]]


_OBJECTS_SQL = """
SELECT
    USER AS schema_name,
    object_row.OBJECT_NAME AS object_name,
    object_row.OBJECT_TYPE AS object_type,
    view_row.TEXT AS definition_sql,
    comment_row.COMMENTS AS object_comment
FROM USER_OBJECTS object_row
LEFT JOIN USER_VIEWS view_row
  ON view_row.VIEW_NAME = object_row.OBJECT_NAME
LEFT JOIN USER_TAB_COMMENTS comment_row
  ON comment_row.TABLE_NAME = object_row.OBJECT_NAME
WHERE object_row.OBJECT_TYPE IN ('TABLE', 'VIEW')
ORDER BY object_row.OBJECT_NAME
"""

_COLUMNS_SQL = """
SELECT
    USER AS schema_name,
    column_row.TABLE_NAME AS object_name,
    column_row.COLUMN_NAME AS column_name,
    column_row.DATA_TYPE AS data_type,
    column_row.DATA_LENGTH AS data_length,
    column_row.CHAR_LENGTH AS char_length,
    column_row.CHAR_USED AS char_used,
    column_row.DATA_PRECISION AS data_precision,
    column_row.DATA_SCALE AS data_scale,
    column_row.DATA_TYPE_OWNER AS data_type_owner,
    column_row.COLUMN_ID AS ordinal,
    column_row.NULLABLE AS is_nullable,
    column_row.DATA_DEFAULT AS default_expression,
    comment_row.COMMENTS AS column_comment
FROM USER_TAB_COLUMNS column_row
LEFT JOIN USER_COL_COMMENTS comment_row
  ON comment_row.TABLE_NAME = column_row.TABLE_NAME
 AND comment_row.COLUMN_NAME = column_row.COLUMN_NAME
ORDER BY column_row.TABLE_NAME, column_row.COLUMN_ID
"""

_PRIMARY_KEYS_SQL = """
SELECT
    USER AS schema_name,
    column_row.TABLE_NAME AS object_name,
    column_row.COLUMN_NAME AS column_name,
    column_row.POSITION AS ordinal
FROM USER_CONSTRAINTS constraint_row
JOIN USER_CONS_COLUMNS column_row
  ON column_row.CONSTRAINT_NAME = constraint_row.CONSTRAINT_NAME
WHERE constraint_row.CONSTRAINT_TYPE = 'P'
ORDER BY column_row.TABLE_NAME, column_row.POSITION
"""

_FOREIGN_KEYS_SQL = """
SELECT
    USER AS source_schema,
    source_column.TABLE_NAME AS source_object,
    USER AS target_schema,
    target_column.TABLE_NAME AS target_object,
    foreign_key.CONSTRAINT_NAME AS constraint_name,
    source_column.COLUMN_NAME AS source_column,
    target_column.COLUMN_NAME AS target_column,
    source_column.POSITION AS ordinal
FROM USER_CONSTRAINTS foreign_key
JOIN USER_CONS_COLUMNS source_column
  ON source_column.CONSTRAINT_NAME = foreign_key.CONSTRAINT_NAME
JOIN USER_CONSTRAINTS referenced_key
  ON referenced_key.CONSTRAINT_NAME = foreign_key.R_CONSTRAINT_NAME
JOIN USER_CONS_COLUMNS target_column
  ON target_column.CONSTRAINT_NAME = referenced_key.CONSTRAINT_NAME
 AND target_column.POSITION = source_column.POSITION
WHERE foreign_key.CONSTRAINT_TYPE = 'R'
ORDER BY source_column.TABLE_NAME, foreign_key.CONSTRAINT_NAME, source_column.POSITION
"""


class OracleConnector:
    """Read-only Oracle metadata connector using python-oracledb Thin mode."""

    def __init__(
        self,
        secret_resolver: OracleSecretResolver,
        connect_factory: OracleConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory

    def capabilities(self, data_source: DataSource) -> frozenset[DataSourceCapability]:
        if data_source.dialect.casefold() != "oracle":
            return frozenset()
        return frozenset({DataSourceCapability.INTROSPECT})

    def introspect(self, data_source: DataSource) -> DataSourceSnapshot:
        self._validate_data_source(data_source)
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")
        secret = self._secret_resolver.resolve_oracle(secret_ref)
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
            raise ConnectorUnavailableError("Oracle introspection failed") from None

        primary_keys: dict[str, set[str]] = {
            _object_ref(row["schema_name"], row["object_name"], upper=True): set()
            for row in primary_key_rows
        }
        for row in primary_key_rows:
            primary_keys[
                _object_ref(row["schema_name"], row["object_name"], upper=True)
            ].add(str(row["column_name"]).casefold())

        columns_by_object: dict[str, list[ColumnSnapshot]] = {}
        for row in column_rows:
            reference = _object_ref(row["schema_name"], row["object_name"], upper=True)
            columns_by_object.setdefault(reference, []).append(
                ColumnSnapshot(
                    name=str(row["column_name"]),
                    physical_type=_physical_type(row),
                    ordinal=int(row["ordinal"]),
                    nullable=str(row["is_nullable"]).casefold() == "y",
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
                    if str(row["object_type"]).casefold() == "view"
                    else ObjectKind.TABLE
                ),
                columns=tuple(
                    columns_by_object.get(
                        _object_ref(row["schema_name"], row["object_name"], upper=True),
                        (),
                    )
                ),
                definition_sql=_optional_text(row["definition_sql"]),
                comment=_optional_text(row["object_comment"]),
            )
            for row in object_rows
        )
        relationships = _relationships(relationship_rows)
        return DataSourceSnapshot(
            data_source_id=data_source.id,
            dialect="oracle",
            objects=objects,
            relationships=relationships,
        )

    def _validate_data_source(self, data_source: DataSource) -> None:
        if data_source.dialect.casefold() != "oracle":
            raise ConnectorConfigurationError("OracleConnector requires Oracle dialect")
        if DataSourceCapability.INTROSPECT not in data_source.capabilities:
            raise ConnectorConfigurationError("DataSource does not allow introspection")
        if data_source.connection_secret_ref is None:
            raise ConnectorConfigurationError("DataSource has no connection secret reference")

    def _connect(self, secret: OracleConnectionSecret) -> AbstractContextManager[OracleConnection]:
        if self._connect_factory is not None:
            return self._connect_factory(**secret.as_connect_kwargs())
        try:
            module = import_module("oracledb")
            connection = module.connect(**secret.as_connect_kwargs())
        except ImportError:
            raise ConnectorUnavailableError(
                "Install the 'oracledb' project dependency"
            ) from None
        except Exception:
            raise ConnectorUnavailableError("Oracle connection failed") from None
        return cast(AbstractContextManager[OracleConnection], connection)


def _fetch_dicts(cursor: OracleCursor, query: str) -> tuple[dict[str, Any], ...]:
    cursor.execute(query)
    names = tuple(
        str(item.name if hasattr(item, "name") else item[0]).casefold()
        for item in cursor.description
    )
    return tuple(dict(zip(names, row, strict=True)) for row in cursor.fetchall())


def _physical_type(row: dict[str, Any]) -> str:
    data_type = str(row["data_type"])
    owner = row.get("data_type_owner")
    if owner:
        return f"{owner}.{data_type}"
    if data_type in {"CHAR", "NCHAR", "NVARCHAR2", "VARCHAR", "VARCHAR2"}:
        if row.get("char_used") == "C":
            return f"{data_type}({int(row['char_length'])} CHAR)"
        return f"{data_type}({int(row['data_length'])})"
    if data_type == "NUMBER" and row.get("data_precision") is not None:
        precision = int(row["data_precision"])
        scale = row.get("data_scale")
        return f"NUMBER({precision},{int(scale)})" if scale is not None else f"NUMBER({precision})"
    if (
        data_type.startswith("TIMESTAMP")
        and "(" not in data_type
        and row.get("data_scale") is not None
    ):
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


def _object_ref(schema_name: object, object_name: object, *, upper: bool) -> str:
    result = f"{schema_name}.{object_name}"
    return result.upper() if upper else result


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
