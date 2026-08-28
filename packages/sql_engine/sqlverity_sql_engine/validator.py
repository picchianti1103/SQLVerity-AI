from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from packages.domain.sqlverity_domain.contracts import (
    SQLProposal,
    ValidationIssue,
    ValidationResult,
)
from packages.domain.sqlverity_domain.models import OutputColumnLineage


@dataclass(frozen=True, slots=True)
class DialectCapabilities:
    dialect: str
    sqlglot_name: str
    supports_cte: bool
    supports_set_operations: bool
    supports_limit: bool
    aliases: frozenset[str] = field(default_factory=frozenset)
    dangerous_functions: frozenset[str] = field(default_factory=frozenset)
    allowed_anonymous_functions: frozenset[str] = field(default_factory=frozenset)


POSTGRESQL_CAPABILITIES = DialectCapabilities(
    dialect="postgresql",
    sqlglot_name="postgres",
    supports_cte=True,
    supports_set_operations=True,
    supports_limit=True,
    aliases=frozenset({"postgres", "postgresql"}),
    dangerous_functions=frozenset(
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
    ),
    allowed_anonymous_functions=frozenset(
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
    ),
)

MYSQL_CAPABILITIES = DialectCapabilities(
    dialect="mysql",
    sqlglot_name="mysql",
    supports_cte=True,
    supports_set_operations=True,
    supports_limit=True,
    aliases=frozenset({"mysql"}),
    dangerous_functions=frozenset(
        {
            "benchmark",
            "get_lock",
            "is_free_lock",
            "is_used_lock",
            "load_file",
            "master_pos_wait",
            "release_all_locks",
            "release_lock",
            "sleep",
            "sys_exec",
            "sys_eval",
        }
    ),
    allowed_anonymous_functions=frozenset(
        {
            "concat_ws",
            "date_format",
            "datediff",
            "extractvalue",
            "find_in_set",
            "from_unixtime",
            "group_concat",
            "json_extract",
            "json_object",
            "json_unquote",
            "last_day",
            "makedate",
            "period_diff",
            "regexp_like",
            "str_to_date",
            "timestampdiff",
            "unix_timestamp",
        }
    ),
)

MARIADB_CAPABILITIES = DialectCapabilities(
    dialect="mariadb",
    sqlglot_name="mysql",
    supports_cte=True,
    supports_set_operations=True,
    supports_limit=True,
    aliases=frozenset({"mariadb"}),
    dangerous_functions=MYSQL_CAPABILITIES.dangerous_functions,
    allowed_anonymous_functions=MYSQL_CAPABILITIES.allowed_anonymous_functions,
)

ORACLE_CAPABILITIES = DialectCapabilities(
    dialect="oracle",
    sqlglot_name="oracle",
    supports_cte=True,
    supports_set_operations=True,
    supports_limit=True,
    aliases=frozenset({"oracle"}),
    dangerous_functions=frozenset(
        {
            "dbms_lock.sleep",
            "request",
            "sleep",
            "utl_file.fopen",
            "utl_http.request",
        }
    ),
    allowed_anonymous_functions=frozenset(
        {
            "add_months",
            "decode",
            "last_day",
            "listagg",
            "months_between",
            "nvl2",
            "regexp_like",
            "regexp_substr",
            "to_char",
            "to_date",
            "to_timestamp",
            "trunc",
        }
    ),
)

SQLSERVER_CAPABILITIES = DialectCapabilities(
    dialect="sqlserver",
    sqlglot_name="tsql",
    supports_cte=True,
    supports_set_operations=True,
    supports_limit=True,
    aliases=frozenset({"mssql", "sqlserver", "tsql"}),
    dangerous_functions=frozenset(
        {
            "openquery",
            "opendatasource",
            "openrowset",
            "xp_cmdshell",
        }
    ),
    allowed_anonymous_functions=frozenset(
        {
            "datefromparts",
            "eomonth",
            "format",
            "iif",
            "parsename",
            "string_agg",
            "try_cast",
            "try_convert",
        }
    ),
)


class UnsupportedDialectError(ValueError):
    pass


class DialectRegistry:
    """Immutable-by-convention registry for SQL dialect capabilities and aliases."""

    def __init__(self, capabilities: Iterable[DialectCapabilities]) -> None:
        self._by_alias: dict[str, DialectCapabilities] = {}
        for item in capabilities:
            aliases = item.aliases or frozenset({item.dialect})
            for alias in aliases | {item.dialect}:
                key = alias.casefold()
                existing = self._by_alias.get(key)
                if existing is not None and existing != item:
                    raise ValueError(f"Duplicate SQL dialect alias: {alias}")
                self._by_alias[key] = item

    def resolve(self, dialect: str) -> DialectCapabilities:
        capabilities = self._by_alias.get(dialect.strip().casefold())
        if capabilities is None:
            raise UnsupportedDialectError(f"Unsupported SQL dialect: {dialect}")
        return capabilities

    def supports(self, dialect: str) -> bool:
        return dialect.strip().casefold() in self._by_alias

    @property
    def dialects(self) -> tuple[str, ...]:
        return tuple(sorted({item.dialect for item in self._by_alias.values()}))


DEFAULT_DIALECT_REGISTRY = DialectRegistry(
    (
        POSTGRESQL_CAPABILITIES,
        MYSQL_CAPABILITIES,
        MARIADB_CAPABILITIES,
        ORACLE_CAPABILITIES,
        SQLSERVER_CAPABILITIES,
    )
)


def sqlglot_dialect_name(dialect: str) -> str:
    return DEFAULT_DIALECT_REGISTRY.resolve(dialect).sqlglot_name


class DialectSQLValidator:
    """Conservative, dialect-aware AST validator for non-executable SQL previews."""

    def __init__(self, capabilities: DialectCapabilities) -> None:
        self.capabilities = capabilities

    def validate(
        self,
        proposal: SQLProposal,
        *,
        allowed_tables: frozenset[str],
        allowed_columns: frozenset[str],
        max_rows: int,
    ) -> ValidationResult:
        if not 1 <= max_rows <= 10_000:
            raise ValueError("max_rows must be between 1 and 10000")
        aliases = self.capabilities.aliases or frozenset({self.capabilities.dialect})
        if proposal.dialect.casefold() not in aliases:
            return _rejected(
                proposal.dialect,
                ValidationIssue(
                    code="unsupported_dialect",
                    message=(
                        f"Validator for {self.capabilities.dialect} cannot validate "
                        f"dialect {proposal.dialect}"
                    ),
                ),
            )
        if proposal.needs_clarification:
            details = "; ".join(proposal.ambiguities)
            return _rejected(
                self.capabilities.dialect,
                ValidationIssue(
                    code="clarification_required",
                    message=(
                        "SQL cannot be validated until the ambiguity is resolved"
                        + (f": {details}" if details else "")
                    ),
                ),
            )

        try:
            statements = sqlglot.parse(proposal.sql, read=self.capabilities.sqlglot_name)
        except ParseError as error:
            return _rejected(
                self.capabilities.dialect,
                ValidationIssue(
                    code="parse_error",
                    message=f"Invalid {self.capabilities.dialect} SQL: {error}",
                ),
            )
        if len(statements) != 1:
            return _rejected(
                self.capabilities.dialect,
                ValidationIssue(
                    code="multiple_statements",
                    message="Exactly one SQL statement is allowed",
                ),
            )
        statement = statements[0]
        if statement is None:
            return _rejected(
                self.capabilities.dialect,
                ValidationIssue(code="parse_error", message="SQL statement is empty"),
            )
        issues: list[ValidationIssue] = []
        if not isinstance(statement, _READ_ONLY_ROOTS):
            _add_issue(
                issues,
                "statement_not_allowed",
                f"Statement {type(statement).__name__} is not a read-only SELECT query",
            )

        prohibited_node = next(statement.find_all(*_PROHIBITED_NODE_TYPES), None)
        if prohibited_node is not None:
            _add_issue(
                issues,
                "write_or_administrative_operation",
                f"SQL contains prohibited operation {type(prohibited_node).__name__}",
            )
        if next(statement.find_all(exp.Into), None) is not None:
            _add_issue(
                issues,
                "select_into_not_allowed",
                "SELECT INTO can create a table and is not allowed",
            )
        if next(statement.find_all(exp.Lock), None) is not None:
            _add_issue(
                issues,
                "locking_clause_not_allowed",
                "Row-locking clauses such as FOR UPDATE are not allowed",
            )
        _validate_parameters(statement, proposal, issues)
        for anonymous in statement.find_all(exp.Anonymous):
            function_name = anonymous.name.casefold()
            if function_name in self.capabilities.dangerous_functions:
                _add_issue(
                    issues,
                    "dangerous_function",
                    f"Function {function_name} is not allowed",
                )
            elif function_name not in self.capabilities.allowed_anonymous_functions:
                _add_issue(
                    issues,
                    "function_not_allowlisted",
                    f"Function {function_name} is not in the read-only allowlist",
                )
        for star in statement.find_all(exp.Star):
            if not _is_count_star(star):
                _add_issue(
                    issues,
                    "wildcard_not_allowed",
                    "Wildcard projections are not allowed; columns must be explicit",
                )

        referenced_tables, referenced_columns = _resolve_references(
            statement,
            allowed_tables,
            allowed_columns,
            issues,
        )
        output_lineage, output_lineage_complete = _resolve_output_lineage(
            statement,
            allowed_tables,
            allowed_columns,
        )
        if frozenset(proposal.tables) != frozenset(referenced_tables):
            _add_issue(
                issues,
                "declared_table_mismatch",
                "Declared tables do not match the SQL AST",
            )
        if frozenset(proposal.columns) != frozenset(referenced_columns):
            _add_issue(
                issues,
                "declared_column_mismatch",
                "Declared columns do not match the SQL AST",
            )

        if any(issue.blocking for issue in issues):
            return ValidationResult(
                dialect=self.capabilities.dialect,
                normalized_sql=None,
                issues=tuple(issues),
                referenced_tables=tuple(sorted(referenced_tables)),
                referenced_columns=tuple(sorted(referenced_columns)),
                output_lineage=output_lineage,
                output_lineage_complete=output_lineage_complete,
            )

        if not isinstance(statement, exp.Query):
            return ValidationResult(
                dialect=self.capabilities.dialect,
                normalized_sql=None,
                issues=(
                    *issues,
                    ValidationIssue(
                        code="statement_not_allowed",
                        message="Only read-only query roots can receive a preview limit",
                    ),
                ),
                referenced_tables=tuple(sorted(referenced_tables)),
                referenced_columns=tuple(sorted(referenced_columns)),
                output_lineage=output_lineage,
                output_lineage_complete=output_lineage_complete,
            )
        limited_statement, limit_issue = _apply_limit(statement, max_rows)
        if limit_issue is not None:
            issues.append(limit_issue)
        if any(issue.blocking for issue in issues):
            return ValidationResult(
                dialect=self.capabilities.dialect,
                normalized_sql=None,
                issues=tuple(issues),
                referenced_tables=tuple(sorted(referenced_tables)),
                referenced_columns=tuple(sorted(referenced_columns)),
                output_lineage=output_lineage,
                output_lineage_complete=output_lineage_complete,
            )
        return ValidationResult(
            dialect=self.capabilities.dialect,
            normalized_sql=limited_statement.sql(dialect=self.capabilities.sqlglot_name),
            issues=tuple(issues),
            referenced_tables=tuple(sorted(referenced_tables)),
            referenced_columns=tuple(sorted(referenced_columns)),
            output_lineage=output_lineage,
            output_lineage_complete=output_lineage_complete,
        )


class PostgreSQLSQLValidator(DialectSQLValidator):
    def __init__(self) -> None:
        super().__init__(POSTGRESQL_CAPABILITIES)


class MySQLSQLValidator(DialectSQLValidator):
    def __init__(self) -> None:
        super().__init__(MYSQL_CAPABILITIES)


class MariaDBSQLValidator(DialectSQLValidator):
    def __init__(self) -> None:
        super().__init__(MARIADB_CAPABILITIES)


class OracleSQLValidator(DialectSQLValidator):
    def __init__(self) -> None:
        super().__init__(ORACLE_CAPABILITIES)


class SQLServerSQLValidator(DialectSQLValidator):
    def __init__(self) -> None:
        super().__init__(SQLSERVER_CAPABILITIES)


class SQLValidatorRegistry:
    """Route validation to the matching dialect without silent fallback."""

    def __init__(
        self,
        validators: Iterable[DialectSQLValidator] | None = None,
    ) -> None:
        selected = tuple(validators or (
            PostgreSQLSQLValidator(),
            MySQLSQLValidator(),
            MariaDBSQLValidator(),
            OracleSQLValidator(),
            SQLServerSQLValidator(),
        ))
        self._validators: dict[str, DialectSQLValidator] = {}
        for validator in selected:
            aliases = validator.capabilities.aliases or frozenset(
                {validator.capabilities.dialect}
            )
            for alias in aliases | {validator.capabilities.dialect}:
                key = alias.casefold()
                if key in self._validators:
                    raise ValueError(f"Duplicate SQL validator alias: {alias}")
                self._validators[key] = validator

    def validate(
        self,
        proposal: SQLProposal,
        *,
        allowed_tables: frozenset[str],
        allowed_columns: frozenset[str],
        max_rows: int,
    ) -> ValidationResult:
        validator = self._validators.get(proposal.dialect.casefold())
        if validator is None:
            return _rejected(
                proposal.dialect,
                ValidationIssue(
                    code="unsupported_dialect",
                    message=f"Unsupported SQL dialect: {proposal.dialect}",
                ),
            )
        return validator.validate(
            proposal,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            max_rows=max_rows,
        )


def _resolve_references(
    statement: exp.Expr,
    allowed_tables: frozenset[str],
    allowed_columns: frozenset[str],
    issues: list[ValidationIssue],
) -> tuple[set[str], set[str]]:
    referenced_tables: set[str] = set()
    referenced_columns: set[str] = set()
    columns_by_table: dict[str, set[str]] = {table: set() for table in allowed_tables}
    for column_ref in allowed_columns:
        if "." not in column_ref:
            continue
        table_ref, column_name = column_ref.rsplit(".", 1)
        columns_by_table.setdefault(table_ref, set()).add(column_name)

    for scope in traverse_scope(statement):
        sources: dict[str, str | None] = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                resolved = _resolve_table(source, allowed_tables, issues)
                sources[alias.casefold()] = resolved
                if resolved is not None:
                    referenced_tables.add(resolved)
            else:
                sources[alias.casefold()] = None

        external_columns = {id(column) for column in scope.external_columns}
        for column in scope.columns:
            if column.is_star:
                continue
            column_name = column.name
            qualifier = column.table
            is_external = id(column) in external_columns
            if is_external and (
                not sources or (qualifier and qualifier.casefold() not in sources)
            ):
                # SQLGlot also reports unresolved local, unqualified columns as
                # external. Only skip references that demonstrably belong to a
                # parent scope; local unqualified columns still need validation.
                continue
            if qualifier:
                key = qualifier.casefold()
                if key not in sources:
                    _add_issue(
                        issues,
                        "unknown_table_alias",
                        f"Column {column.sql()} uses an unknown table alias",
                    )
                    continue
                qualified_table_ref = sources[key]
                if qualified_table_ref is None:
                    continue
                if not _column_allowed(
                    qualified_table_ref,
                    column_name,
                    columns_by_table,
                ):
                    _add_issue(
                        issues,
                        "column_not_allowed",
                        (
                            f"Column {qualified_table_ref}.{column_name} "
                            "is outside the allowed context"
                        ),
                    )
                    continue
                referenced_columns.add(
                    _canonical_column(
                        qualified_table_ref,
                        column_name,
                        columns_by_table,
                    )
                )
                continue

            physical_sources = frozenset(
                source for source in sources.values() if source is not None
            )
            has_derived_source = any(source is None for source in sources.values())
            if has_derived_source and physical_sources:
                _add_issue(
                    issues,
                    "ambiguous_derived_column",
                    f"Unqualified column {column_name} mixes physical and derived sources",
                )
                continue
            if has_derived_source and not physical_sources:
                continue
            matches = tuple(
                table_ref
                for table_ref in physical_sources
                if _column_allowed(table_ref, column_name, columns_by_table)
            )
            if len(matches) == 1:
                referenced_columns.add(
                    _canonical_column(matches[0], column_name, columns_by_table)
                )
            elif not matches:
                _add_issue(
                    issues,
                    "column_not_allowed",
                    f"Column {column_name} is outside the allowed context",
                )
            else:
                candidate_refs = ", ".join(
                    sorted(
                        (
                            f"{table_ref}."
                            f"{_canonical_column_name(table_ref, column_name, columns_by_table)}"
                        )
                        for table_ref in matches
                    )
                )
                _add_issue(
                    issues,
                    "ambiguous_column",
                    (
                        f"Unqualified column {column_name} matches multiple tables; "
                        f"candidates: {candidate_refs}"
                    ),
                )
    return referenced_tables, referenced_columns


def _validate_parameters(
    statement: exp.Expr,
    proposal: SQLProposal,
    issues: list[ValidationIssue],
) -> None:
    declarations = proposal.parameters
    if len(declarations) > 50:
        _add_issue(
            issues,
            "too_many_parameters",
            "A generated query can declare at most 50 parameters",
        )
    declared_names = {definition.name for definition in declarations}
    positional = tuple(statement.find_all(exp.Parameter))
    placeholders = tuple(statement.find_all(exp.Placeholder))
    invalid_placeholders = tuple(
        placeholder
        for placeholder in placeholders
        if placeholder.name == "?"
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", placeholder.name)
    )
    if positional or invalid_placeholders:
        _add_issue(
            issues,
            "parameter_syntax_not_allowed",
            "Use only governed named :parameter placeholders",
        )
    used_names = {
        placeholder.name
        for placeholder in placeholders
        if placeholder not in invalid_placeholders
    }
    if used_names != declared_names:
        missing = ", ".join(sorted(declared_names - used_names)) or "none"
        undeclared = ", ".join(sorted(used_names - declared_names)) or "none"
        _add_issue(
            issues,
            "parameter_declaration_mismatch",
            (
                "SQL placeholders do not match governed declarations; "
                f"unused declarations: {missing}; undeclared placeholders: {undeclared}"
            ),
        )
    for placeholder in placeholders:
        parent = placeholder.parent
        while parent is not None:
            if isinstance(parent, (exp.Limit, exp.Offset)):
                _add_issue(
                    issues,
                    "parameter_position_not_allowed",
                    "Preview LIMIT and OFFSET cannot be parameterized",
                )
                break
            parent = parent.parent


def _resolve_output_lineage(
    statement: exp.Expr,
    allowed_tables: frozenset[str],
    allowed_columns: frozenset[str],
) -> tuple[tuple[OutputColumnLineage, ...], bool]:
    columns_by_table: dict[str, set[str]] = {table: set() for table in allowed_tables}
    for column_ref in allowed_columns:
        if "." in column_ref:
            table_ref, column_name = column_ref.rsplit(".", 1)
            columns_by_table.setdefault(table_ref, set()).add(column_name)

    scope_outputs: dict[int, dict[str, tuple[frozenset[str], bool]]] = {}
    root_output: tuple[OutputColumnLineage, ...] = ()
    root_complete = False
    scopes = tuple(traverse_scope(statement))
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            scope_outputs[id(scope)] = {}
            continue
        sources: dict[
            str,
            str | dict[str, tuple[frozenset[str], bool]] | None,
        ] = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                sources[alias.casefold()] = _resolve_table_silent(source, allowed_tables)
            elif isinstance(source, Scope):
                sources[alias.casefold()] = scope_outputs.get(id(source))
            else:
                sources[alias.casefold()] = None

        current_columns = {id(column): column for column in scope.columns}
        output_map: dict[str, tuple[frozenset[str], bool]] = {}
        output_items: list[OutputColumnLineage] = []
        complete = True
        for projection in scope.expression.selects:
            output_name = projection.alias_or_name
            if not output_name or output_name.casefold() in output_map:
                complete = False
                continue
            source_refs: set[str] = set()
            projection_complete = True
            projection_columns = tuple(
                column
                for column in projection.find_all(exp.Column)
                if id(column) in current_columns and not column.is_star
            )
            for column in projection_columns:
                resolved = _resolve_output_column(
                    column,
                    sources,
                    columns_by_table,
                )
                if resolved is None:
                    projection_complete = False
                else:
                    source_refs.update(resolved)
            canonical_sources = frozenset(source_refs)
            output_map[output_name.casefold()] = (
                canonical_sources,
                projection_complete,
            )
            output_items.append(
                OutputColumnLineage(
                    output_name=output_name,
                    source_columns=tuple(sorted(canonical_sources)),
                )
            )
            complete = complete and projection_complete
        if len(output_items) != len(scope.expression.selects):
            complete = False
        scope_outputs[id(scope)] = output_map
        if scope is scopes[-1]:
            root_output = tuple(output_items)
            root_complete = complete
    return root_output, root_complete


def _resolve_output_column(
    column: exp.Column,
    sources: dict[str, str | dict[str, tuple[frozenset[str], bool]] | None],
    columns_by_table: dict[str, set[str]],
) -> frozenset[str] | None:
    if column.table:
        source = sources.get(column.table.casefold())
        return _lineage_from_source(source, column.name, columns_by_table)
    matches = tuple(
        lineage
        for source in sources.values()
        if (lineage := _lineage_from_source(source, column.name, columns_by_table))
        is not None
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _lineage_from_source(
    source: str | dict[str, tuple[frozenset[str], bool]] | None,
    column_name: str,
    columns_by_table: dict[str, set[str]],
) -> frozenset[str] | None:
    if isinstance(source, str):
        if not _column_allowed(source, column_name, columns_by_table):
            return None
        return frozenset({_canonical_column(source, column_name, columns_by_table)})
    if isinstance(source, dict):
        resolved = source.get(column_name.casefold())
        if resolved is None or not resolved[1]:
            return None
        return resolved[0]
    return None


def _resolve_table_silent(
    table: exp.Table,
    allowed_tables: frozenset[str],
) -> str | None:
    if table.catalog or not table.name:
        return None
    if table.db:
        matches = _casefold_matches(f"{table.db}.{table.name}", allowed_tables)
    else:
        matches = tuple(
            candidate
            for candidate in allowed_tables
            if candidate.rsplit(".", 1)[-1].casefold() == table.name.casefold()
        )
    return matches[0] if len(matches) == 1 else None


def _resolve_table(
    table: exp.Table,
    allowed_tables: frozenset[str],
    issues: list[ValidationIssue],
) -> str | None:
    if table.catalog:
        _add_issue(
            issues,
            "cross_catalog_reference",
            f"Cross-catalog reference {table.sql()} is not allowed",
        )
        return None
    if not table.name:
        _add_issue(issues, "invalid_table_reference", "Table reference has no name")
        return None
    if table.db:
        requested = f"{table.db}.{table.name}"
        matches = _casefold_matches(requested, allowed_tables)
    else:
        matches = tuple(
            candidate
            for candidate in allowed_tables
            if candidate.rsplit(".", 1)[-1].casefold() == table.name.casefold()
        )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        _add_issue(
            issues,
            "table_not_allowed",
            f"Table {table.sql()} is outside the allowed context",
        )
    else:
        candidates = ", ".join(sorted(matches))
        _add_issue(
            issues,
            "ambiguous_table",
            (
                f"Unqualified table {table.name} matches multiple schemas; "
                f"candidates: {candidates}"
            ),
        )
    return None


def _casefold_matches(value: str, candidates: frozenset[str]) -> tuple[str, ...]:
    return tuple(candidate for candidate in candidates if candidate.casefold() == value.casefold())


def _column_allowed(
    table_ref: str,
    column_name: str,
    columns_by_table: dict[str, set[str]],
) -> bool:
    return any(
        candidate.casefold() == column_name.casefold()
        for candidate in columns_by_table.get(table_ref, set())
    )


def _canonical_column(
    table_ref: str,
    column_name: str,
    columns_by_table: dict[str, set[str]],
) -> str:
    canonical_name = next(
        candidate
        for candidate in columns_by_table[table_ref]
        if candidate.casefold() == column_name.casefold()
    )
    return f"{table_ref}.{canonical_name}"


def _canonical_column_name(
    table_ref: str,
    column_name: str,
    columns_by_table: dict[str, set[str]],
) -> str:
    return next(
        candidate
        for candidate in columns_by_table[table_ref]
        if candidate.casefold() == column_name.casefold()
    )


def _is_count_star(star: exp.Star) -> bool:
    parent = star.parent
    if isinstance(parent, exp.Count):
        return True
    return isinstance(parent, exp.Column) and isinstance(parent.parent, exp.Count)


def _apply_limit(
    statement: exp.Query,
    max_rows: int,
) -> tuple[exp.Query, ValidationIssue | None]:
    limit = statement.args.get("limit")
    if limit is None:
        return statement.copy().limit(max_rows), ValidationIssue(
            code="limit_added",
            message=f"LIMIT {max_rows} was added for preview safety",
            blocking=False,
        )
    count = limit.expression if isinstance(limit, exp.Limit) else limit.args.get("count")
    if not isinstance(count, exp.Literal) or not count.is_int:
        return statement, ValidationIssue(
            code="dynamic_limit_not_allowed",
            message="Preview LIMIT must be a static integer",
        )
    requested = int(count.this)
    limit_options = limit.args.get("limit_options")
    with_ties = bool(limit_options and limit_options.args.get("with_ties"))
    if requested > max_rows or with_ties:
        return statement.copy().limit(min(requested, max_rows)), ValidationIssue(
            code="limit_capped",
            message=f"Preview row limit was capped at {max_rows}",
            blocking=False,
        )
    return statement.copy(), None


def _add_issue(
    issues: list[ValidationIssue],
    code: str,
    message: str,
) -> None:
    if any(issue.code == code and issue.message == message for issue in issues):
        return
    issues.append(ValidationIssue(code=code, message=message))


def _rejected(dialect: str, issue: ValidationIssue) -> ValidationResult:
    return ValidationResult(dialect=dialect, normalized_sql=None, issues=(issue,))


_READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

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
