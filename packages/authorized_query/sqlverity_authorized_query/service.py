from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from packages.catalog.sqlverity_catalog.ingestion import (
    CatalogIngestionService,
    IngestionReport,
)
from packages.domain.sqlverity_domain.contracts import (
    ColumnSnapshot,
    DataSourceSnapshot,
    SchemaObjectSnapshot,
)
from packages.domain.sqlverity_domain.models import (
    AuthorizedQueryDefinition,
    AuthorizedQueryParameter,
    DataSource,
    DataSourceCapability,
    DataSourceType,
    ObjectKind,
)


class AuthorizedQueryError(RuntimeError):
    pass


class AuthorizedQueryConfigurationError(AuthorizedQueryError):
    pass


class AuthorizedQueryDataSourceNotFoundError(AuthorizedQueryError):
    pass


class AuthorizedQueryMaterializationError(AuthorizedQueryError):
    pass


class AuthorizedQueryCatalog(Protocol):
    def get_data_source(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> DataSource | None: ...

    def get_latest_authorized_query_definition(
        self,
        tenant_id: str,
        data_source_id: str,
    ) -> AuthorizedQueryDefinition | None: ...

    def create_authorized_query_definition(
        self,
        definition: AuthorizedQueryDefinition,
    ) -> AuthorizedQueryDefinition: ...


@dataclass(frozen=True, slots=True)
class PreparedAuthorizedQuery:
    sql: str
    parameters: Mapping[str, Any]
    parameter_names: tuple[str, ...]
    parameter_value_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class AuthorizedQueryRegistration:
    definition: AuthorizedQueryDefinition
    ingestion: IngestionReport


class AuthorizedQueryCompiler:
    def validate_base_query(
        self,
        *,
        base_sql: str,
        parameters: tuple[AuthorizedQueryParameter, ...],
        output_columns: tuple[ColumnSnapshot, ...],
        dialect: str,
    ) -> str:
        if dialect.casefold() not in {"postgres", "postgresql"}:
            raise AuthorizedQueryConfigurationError(
                "Authorized query compilation currently supports PostgreSQL only"
            )
        statement = _parse_one(base_sql, AuthorizedQueryConfigurationError)
        if not isinstance(statement, exp.Select):
            raise AuthorizedQueryConfigurationError(
                "Authorized base SQL must be one SELECT query"
            )
        _validate_read_only_base(statement)
        expected_columns = tuple(column.name for column in output_columns)
        if not expected_columns:
            raise AuthorizedQueryConfigurationError(
                "Authorized query output schema must contain at least one column"
            )
        if any(not _safe_identifier(name) for name in expected_columns):
            raise AuthorizedQueryConfigurationError(
                "Authorized query output column names must be safe identifiers"
            )
        if len(expected_columns) != len({name.casefold() for name in expected_columns}):
            raise AuthorizedQueryConfigurationError(
                "Authorized query output column names must be unique"
            )
        actual_columns = tuple(projection.alias_or_name for projection in statement.expressions)
        if any(not name for name in actual_columns):
            raise AuthorizedQueryConfigurationError(
                "Every authorized query output expression must have a stable name or alias"
            )
        if tuple(name.casefold() for name in actual_columns) != tuple(
            name.casefold() for name in expected_columns
        ):
            raise AuthorizedQueryConfigurationError(
                "Authorized base query projections must match the declared output schema in order"
            )
        declared_parameters = {parameter.name for parameter in parameters}
        unsupported_types = sorted(
            parameter.physical_type
            for parameter in parameters
            if _parameter_type_category(parameter.physical_type) is None
        )
        if unsupported_types:
            raise AuthorizedQueryConfigurationError(
                "Authorized query parameter types are unsupported in the scalar binder: "
                f"{unsupported_types}"
            )
        used_parameters = {placeholder.name for placeholder in statement.find_all(exp.Placeholder)}
        if next(statement.find_all(exp.Parameter), None) is not None:
            raise AuthorizedQueryConfigurationError(
                "Use named :parameter placeholders in authorized base SQL"
            )
        if used_parameters != declared_parameters:
            missing = sorted(declared_parameters - used_parameters)
            undeclared = sorted(used_parameters - declared_parameters)
            raise AuthorizedQueryConfigurationError(
                "Authorized base query parameters do not match declarations"
                f"; unused={missing}; undeclared={undeclared}"
            )
        return statement.sql(dialect="postgres")

    def materialize(
        self,
        *,
        definition: AuthorizedQueryDefinition,
        outer_sql: str,
        parameters: Mapping[str, Any],
    ) -> PreparedAuthorizedQuery:
        statement = _parse_one(outer_sql, AuthorizedQueryMaterializationError)
        if not isinstance(statement, exp.Query):
            raise AuthorizedQueryMaterializationError(
                "Authorized outer SQL must be a read-only query"
            )
        if next(statement.find_all(exp.Placeholder, exp.Parameter), None) is not None:
            raise AuthorizedQueryMaterializationError(
                "Outer SQL cannot introduce its own parameters"
            )
        if not definition.allow_filtering and next(
            statement.find_all(exp.Where, exp.Having, exp.Qualify),
            None,
        ) is not None:
            raise AuthorizedQueryMaterializationError(
                "Filtering is disabled by the authorized query policy"
            )
        if not definition.allow_aggregation and next(
            statement.find_all(exp.AggFunc, exp.Group),
            None,
        ) is not None:
            raise AuthorizedQueryMaterializationError(
                "Aggregation is disabled by the authorized query policy"
            )

        matches = tuple(
            table
            for table in statement.find_all(exp.Table)
            if _matches_virtual_table(table, definition)
        )
        if len(matches) != 1:
            raise AuthorizedQueryMaterializationError(
                "Outer SQL must reference the authorized virtual table exactly once"
            )
        bindings = _validate_bindings(definition.parameters, parameters)
        base_statement = _parse_one(
            definition.normalized_base_sql,
            AuthorizedQueryMaterializationError,
        )
        table = matches[0]
        alias_name = table.alias or table.name
        table.replace(
            exp.Subquery(
                this=base_statement,
                alias=exp.TableAlias(this=exp.to_identifier(alias_name)),
            )
        )
        parameter_names = tuple(sorted(bindings))
        signature_payload = {
            "definition_id": definition.id,
            "parameters": {name: bindings[name] for name in parameter_names},
        }
        try:
            serialized = json.dumps(
                signature_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise AuthorizedQueryMaterializationError(
                "Authorized query parameter values are not canonical JSON scalars"
            ) from error
        return PreparedAuthorizedQuery(
            sql=statement.sql(dialect="postgres"),
            parameters=bindings,
            parameter_names=parameter_names,
            parameter_value_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        )


class AuthorizedQueryService:
    def __init__(
        self,
        catalog: AuthorizedQueryCatalog,
        ingestion: CatalogIngestionService,
        compiler: AuthorizedQueryCompiler | None = None,
    ) -> None:
        self._catalog = catalog
        self._ingestion = ingestion
        self._compiler = compiler or AuthorizedQueryCompiler()

    def register(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        virtual_schema: str,
        virtual_name: str,
        description: str,
        base_sql: str,
        parameters: tuple[AuthorizedQueryParameter, ...],
        output_columns: tuple[ColumnSnapshot, ...],
        allow_filtering: bool = True,
        allow_aggregation: bool = True,
    ) -> AuthorizedQueryRegistration:
        data_source = self._catalog.get_data_source(tenant_id, data_source_id)
        if data_source is None:
            raise AuthorizedQueryDataSourceNotFoundError(
                "DataSource does not exist in this tenant"
            )
        _require_authorized_data_source(data_source)
        normalized = self._compiler.validate_base_query(
            base_sql=base_sql,
            parameters=parameters,
            output_columns=output_columns,
            dialect=data_source.dialect,
        )
        latest = self._catalog.get_latest_authorized_query_definition(
            tenant_id,
            data_source_id,
        )
        version = 1 if latest is None else latest.version + 1
        draft = AuthorizedQueryDefinition(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog_version_id="pending",
            version=version,
            virtual_schema=virtual_schema,
            virtual_name=virtual_name,
            description=description,
            base_sql=base_sql,
            normalized_base_sql=normalized,
            parameters=parameters,
            allow_filtering=allow_filtering,
            allow_aggregation=allow_aggregation,
        )
        ingestion = self._ingestion.ingest_snapshot(
            tenant_id,
            data_source_id,
            DataSourceSnapshot(
                data_source_id=data_source_id,
                dialect=data_source.dialect,
                objects=(
                    SchemaObjectSnapshot(
                        schema_name=virtual_schema,
                        name=virtual_name,
                        kind=ObjectKind.VIRTUAL_QUERY,
                        columns=output_columns,
                        definition_sql=normalized,
                        comment=description,
                    ),
                ),
            ),
            semantic_source="authorized_query_definition",
        )
        definition = self._catalog.create_authorized_query_definition(
            replace(draft, catalog_version_id=ingestion.catalog_version_id)
        )
        return AuthorizedQueryRegistration(definition=definition, ingestion=ingestion)


def _require_authorized_data_source(data_source: DataSource) -> None:
    if data_source.source_type is not DataSourceType.AUTHORIZED_QUERY:
        raise AuthorizedQueryConfigurationError(
            "Authorized query definitions require an authorized_query DataSource"
        )
    required = {
        DataSourceCapability.EXPLAIN,
        DataSourceCapability.EXECUTE_READ_ONLY,
    }
    if not required.issubset(data_source.capabilities):
        raise AuthorizedQueryConfigurationError(
            "Authorized query DataSource requires explain and execute_read_only capabilities"
        )
    if data_source.connection_secret_ref is None:
        raise AuthorizedQueryConfigurationError(
            "Authorized query DataSource requires a connection secret reference"
        )


def _parse_one(
    sql: str,
    error_type: type[AuthorizedQueryConfigurationError]
    | type[AuthorizedQueryMaterializationError],
) -> exp.Expr:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError as error:
        raise error_type(f"Invalid PostgreSQL SQL: {error}") from error
    if len(statements) != 1 or statements[0] is None:
        raise error_type("Exactly one SQL statement is required")
    return statements[0]


def _validate_read_only_base(statement: exp.Select) -> None:
    prohibited = next(statement.find_all(*_PROHIBITED_NODE_TYPES), None)
    if prohibited is not None:
        raise AuthorizedQueryConfigurationError(
            f"Authorized base query contains prohibited {type(prohibited).__name__}"
        )
    if next(statement.find_all(exp.Into, exp.Lock), None) is not None:
        raise AuthorizedQueryConfigurationError(
            "Authorized base query cannot use SELECT INTO or row locks"
        )
    if next(statement.find_all(exp.Star), None) is not None:
        raise AuthorizedQueryConfigurationError(
            "Authorized base query output columns must be explicit"
        )
    if any(table.catalog for table in statement.find_all(exp.Table)):
        raise AuthorizedQueryConfigurationError(
            "Authorized base query cannot reference another catalog"
        )
    for function in statement.find_all(exp.Anonymous):
        function_name = function.name.casefold()
        if function_name in _DANGEROUS_FUNCTIONS:
            raise AuthorizedQueryConfigurationError(
                f"Function {function.name} is not allowed in an authorized base query"
            )
        if function_name not in _ALLOWED_ANONYMOUS_FUNCTIONS:
            raise AuthorizedQueryConfigurationError(
                f"Function {function.name} is not in the read-only allowlist"
            )


def _matches_virtual_table(
    table: exp.Table,
    definition: AuthorizedQueryDefinition,
) -> bool:
    if table.catalog:
        return False
    if table.db:
        candidate = f"{table.db}.{table.name}"
        return candidate.casefold() == definition.virtual_object_ref.casefold()
    return table.name.casefold() == definition.virtual_name.casefold()


def _validate_bindings(
    declared: tuple[AuthorizedQueryParameter, ...],
    supplied: Mapping[str, Any],
) -> dict[str, Any]:
    declared_by_name = {parameter.name: parameter for parameter in declared}
    if set(supplied) != set(declared_by_name):
        missing = sorted(set(declared_by_name) - set(supplied))
        unexpected = sorted(set(supplied) - set(declared_by_name))
        raise AuthorizedQueryMaterializationError(
            f"Authorized query parameter mismatch; missing={missing}; unexpected={unexpected}"
        )
    bindings: dict[str, Any] = {}
    for name, parameter in declared_by_name.items():
        value = supplied[name]
        if value is None and not parameter.nullable:
            raise AuthorizedQueryMaterializationError(
                f"Authorized query parameter {name} cannot be null"
            )
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise AuthorizedQueryMaterializationError(
                f"Authorized query parameter {name} must be a JSON scalar"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise AuthorizedQueryMaterializationError(
                f"Authorized query parameter {name} must be finite"
            )
        if value is not None and not _matches_physical_type(value, parameter.physical_type):
            raise AuthorizedQueryMaterializationError(
                f"Authorized query parameter {name} does not match {parameter.physical_type}"
            )
        bindings[name] = value
    return bindings


def _matches_physical_type(value: object, physical_type: str) -> bool:
    category = _parameter_type_category(physical_type)
    if category == "boolean":
        return isinstance(value, bool)
    if category == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if category == "numeric":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        try:
            return Decimal(str(value)).is_finite()
        except InvalidOperation:
            return False
    if category == "string":
        return isinstance(value, str)
    return False


def _parameter_type_category(physical_type: str) -> str | None:
    normalized = physical_type.strip().casefold()
    if "[]" in normalized:
        return None
    base_type = normalized.split("(", 1)[0].split()[0]
    if base_type in {"bool", "boolean"}:
        return "boolean"
    if base_type in {"smallint", "int2", "integer", "int", "int4", "bigint", "int8"}:
        return "integer"
    if base_type in {
        "decimal",
        "numeric",
        "real",
        "float4",
        "double",
        "float8",
        "money",
    }:
        return "numeric"
    if base_type in {
        "date",
        "time",
        "timetz",
        "timestamp",
        "timestamptz",
        "uuid",
        "text",
        "varchar",
        "character",
        "char",
    }:
        return "string"
    return None


def _safe_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 63
        and (value[0].isascii() and (value[0].isalpha() or value[0] == "_"))
        and all(
            character.isascii() and (character.isalnum() or character == "_")
            for character in value
        )
    )


_PROHIBITED_NODE_TYPES = (
    exp.Alter,
    exp.Analyze,
    exp.Cache,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Execute,
    exp.Grant,
    exp.Insert,
    exp.LoadData,
    exp.Merge,
    exp.Revoke,
    exp.Set,
    exp.Transaction,
    exp.TruncateTable,
    exp.Uncache,
    exp.Update,
    exp.Use,
)

_DANGEROUS_FUNCTIONS = frozenset(
    {
        "dblink",
        "dblink_exec",
        "lo_export",
        "lo_import",
        "nextval",
        "pg_advisory_lock",
        "pg_advisory_unlock",
        "pg_cancel_backend",
        "pg_ls_dir",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_sleep",
        "pg_stat_file",
        "pg_terminate_backend",
        "set_config",
        "setval",
    }
)

_ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    {
        "age",
        "array_to_string",
        "concat_ws",
        "date_bin",
        "date_part",
        "json_build_array",
        "json_build_object",
        "json_extract_path_text",
        "jsonb_build_array",
        "jsonb_build_object",
        "jsonb_extract_path_text",
        "make_date",
        "make_interval",
        "make_time",
        "make_timestamp",
        "make_timestamptz",
        "regexp_matches",
        "regexp_split_to_array",
        "split_part",
        "string_to_array",
        "timezone",
        "width_bucket",
    }
)
