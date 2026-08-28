from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from importlib import import_module
from threading import RLock
from time import perf_counter
from typing import Any, Protocol, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from packages.domain.sqlverity_domain.contracts import ExplainResult, ReadOnlyResult
from packages.domain.sqlverity_domain.models import DataSource, DataSourceCapability

from .connection import MySQLConnectionSecret, MySQLSecretResolver
from .postgresql_executor import (
    ExecutionCursor,
    ReadOnlyExecutionError,
    ReadOnlyExecutorConfigurationError,
    ReadOnlyExecutorUnavailableError,
    _column_names,
    _elapsed_ms,
    _fetch_bounded_rows,
)


class MySQLExecutionConnection(Protocol):
    connection_id: int

    def cursor(self, **kwargs: Any) -> ExecutionCursor: ...


MySQLExecutionConnectFactory = Callable[
    ...,
    AbstractContextManager[MySQLExecutionConnection],
]


@dataclass(frozen=True, slots=True)
class _ActiveQuery:
    secret: MySQLConnectionSecret
    connection_id: int


class MySQLReadOnlyExecutor:
    """Bounded read-only execution for MySQL or MariaDB with KILL QUERY cancellation."""

    def __init__(
        self,
        secret_resolver: MySQLSecretResolver,
        connect_factory: MySQLExecutionConnectFactory | None = None,
        *,
        dialect: str = "mysql",
    ) -> None:
        normalized = dialect.casefold()
        if normalized not in {"mysql", "mariadb"}:
            raise ValueError("MySQLReadOnlyExecutor dialect must be mysql or mariadb")
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory
        self._dialect = normalized
        self._active_lock = RLock()
        self._active: dict[str, _ActiveQuery] = {}

    def explain(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
    ) -> ExplainResult:
        self._validate_request(
            data_source,
            request_id,
            sql,
            timeout_seconds,
            DataSourceCapability.EXPLAIN,
        )
        secret = self._resolve_secret(data_source)
        driver_sql = _mysql_driver_sql(sql, parameters, self._dialect)
        started = perf_counter()
        try:
            with self._connect(secret) as connection:
                active = self._register(request_id, connection, secret)
                try:
                    with connection.cursor(buffered=True) as cursor:
                        self._configure_read_only(cursor, timeout_seconds)
                        cursor.execute(
                            "EXPLAIN FORMAT=JSON " + driver_sql,
                            dict(parameters) if parameters else None,
                        )
                        row = cursor.fetchone()
                finally:
                    self._unregister(request_id, active)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError(f"{self._dialect} EXPLAIN failed") from None
        plan = _parse_explain_row(row, self._dialect)
        estimated_cost = _find_float(plan, ("query_cost", "cost"))
        estimated_rows = _find_int(
            plan,
            ("rows_produced_per_join", "rows_examined_per_scan", "rows"),
        )
        return ExplainResult(
            plan=plan,
            estimated_total_cost=estimated_cost,
            estimated_rows=estimated_rows,
            elapsed_ms=_elapsed_ms(started),
        )

    def execute_read_only(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
    ) -> ReadOnlyResult:
        self._validate_request(
            data_source,
            request_id,
            sql,
            timeout_seconds,
            DataSourceCapability.EXECUTE_READ_ONLY,
        )
        if not 1 <= max_rows <= 10_000:
            raise ReadOnlyExecutorConfigurationError("max_rows must be between 1 and 10000")
        if not 1_024 <= max_result_bytes <= 100_000_000:
            raise ReadOnlyExecutorConfigurationError(
                "max_result_bytes must be between 1024 and 100000000"
            )
        secret = self._resolve_secret(data_source)
        driver_sql = _mysql_driver_sql(sql, parameters, self._dialect)
        started = perf_counter()
        try:
            with self._connect(secret) as connection:
                active = self._register(request_id, connection, secret)
                try:
                    with connection.cursor(buffered=False) as cursor:
                        self._configure_read_only(cursor, timeout_seconds)
                        cursor.execute(
                            driver_sql,
                            dict(parameters) if parameters else None,
                        )
                        columns = _column_names(cursor.description)
                        rows, result_bytes, truncation_reason = _fetch_bounded_rows(
                            cursor,
                            columns,
                            max_rows,
                            max_result_bytes,
                        )
                finally:
                    self._unregister(request_id, active)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError(
                f"{self._dialect} read-only execution failed"
            ) from None
        return ReadOnlyResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncation_reason is not None,
            truncation_reason=truncation_reason,
            result_bytes=result_bytes,
            elapsed_ms=_elapsed_ms(started),
        )

    def cancel(self, request_id: str) -> bool:
        if not request_id.strip():
            return False
        with self._active_lock:
            active = self._active.get(request_id)
        if active is None:
            return False
        try:
            with self._connect(active.secret) as control_connection:
                with control_connection.cursor(buffered=True) as cursor:
                    cursor.execute(f"KILL QUERY {active.connection_id}")
        except Exception:
            return False
        return True


    def _validate_request(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        timeout_seconds: int,
        capability: DataSourceCapability,
    ) -> None:
        if data_source.dialect.casefold() != self._dialect:
            raise ReadOnlyExecutorConfigurationError(
                f"Executor configured for {self._dialect} cannot handle {data_source.dialect}"
            )
        if capability not in data_source.capabilities:
            raise ReadOnlyExecutorConfigurationError(
                f"DataSource does not allow {capability.value}"
            )
        if data_source.connection_secret_ref is None:
            raise ReadOnlyExecutorConfigurationError(
                "DataSource has no connection secret reference"
            )
        if not request_id.strip() or not sql.strip():
            raise ReadOnlyExecutorConfigurationError("Execution requires request id and SQL")
        if not 1 <= timeout_seconds <= 300:
            raise ReadOnlyExecutorConfigurationError(
                "timeout_seconds must be between 1 and 300"
            )

    def _resolve_secret(self, data_source: DataSource) -> MySQLConnectionSecret:
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ReadOnlyExecutorConfigurationError(
                "DataSource has no connection secret reference"
            )
        return self._secret_resolver.resolve_mysql(secret_ref)

    def _connect(
        self,
        secret: MySQLConnectionSecret,
    ) -> AbstractContextManager[MySQLExecutionConnection]:
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
                raise ReadOnlyExecutorUnavailableError(
                    "Install an organization-approved patched 'mariadb' driver separately"
                ) from None
            try:
                connection = mariadb.connect(**secret.as_mariadb_connect_kwargs())
            except Exception:
                raise ReadOnlyExecutorUnavailableError("mariadb connection failed") from None
            return cast(AbstractContextManager[MySQLExecutionConnection], connection)
        try:
            import mysql.connector
        except ImportError:
            raise ReadOnlyExecutorUnavailableError(
                "Install the 'mysql-connector-python' project dependency"
            ) from None
        try:
            connection = mysql.connector.connect(**secret.as_connect_kwargs())
        except Exception:
            raise ReadOnlyExecutorUnavailableError(
                f"{self._dialect} connection failed"
            ) from None
        return cast(AbstractContextManager[MySQLExecutionConnection], connection)

    def _configure_read_only(self, cursor: Any, timeout_seconds: int) -> None:
        if self._dialect == "mysql":
            cursor.execute(
                "SET SESSION MAX_EXECUTION_TIME = %s",
                (timeout_seconds * 1_000,),
            )
        else:
            cursor.execute(
                "SET SESSION max_statement_time = %s",
                (timeout_seconds,),
            )
        cursor.execute("START TRANSACTION READ ONLY")

    def _register(
        self,
        request_id: str,
        connection: MySQLExecutionConnection,
        secret: MySQLConnectionSecret,
    ) -> _ActiveQuery:
        raw_connection_id = getattr(connection, "connection_id", None)
        if callable(raw_connection_id):
            raw_connection_id = raw_connection_id()
        if not isinstance(raw_connection_id, int) or raw_connection_id <= 0:
            raise ReadOnlyExecutionError(
                f"{self._dialect} connection did not expose a valid connection id"
            )
        active = _ActiveQuery(secret=secret, connection_id=raw_connection_id)
        with self._active_lock:
            if request_id in self._active:
                raise ReadOnlyExecutionError("Query request is already active")
            self._active[request_id] = active
        return active

    def _unregister(self, request_id: str, active: _ActiveQuery) -> None:
        with self._active_lock:
            if self._active.get(request_id) == active:
                del self._active[request_id]


def _mysql_driver_sql(
    sql: str,
    parameters: Mapping[str, Any],
    dialect: str,
) -> str:
    if not parameters:
        return sql
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except ParseError:
        raise ReadOnlyExecutorConfigurationError(
            f"{dialect} parameterized SQL could not be parsed"
        ) from None
    used_names: list[str] = []

    def replace_placeholder(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.Placeholder):
            return node
        if node.name == "?":
            raise ReadOnlyExecutorConfigurationError(
                f"{dialect} requires governed named parameters"
            )
        used_names.append(node.name)
        return exp.Var(this=f"%({node.name})s")

    driver_statement = statement.transform(replace_placeholder)
    if set(used_names) != set(parameters):
        raise ReadOnlyExecutorConfigurationError(
            f"{dialect} SQL placeholders do not match supplied parameters"
        )
    return driver_statement.sql(dialect="mysql")


def _parse_explain_row(row: Any, dialect: str) -> Mapping[str, Any]:
    if not row:
        raise ReadOnlyExecutionError(f"{dialect} EXPLAIN returned no plan")
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ReadOnlyExecutionError(f"{dialect} EXPLAIN returned invalid JSON") from None
    if isinstance(raw, Mapping):
        return dict(raw)
    raise ReadOnlyExecutionError(f"{dialect} EXPLAIN returned an invalid plan")


def _find_float(value: object, keys: tuple[str, ...]) -> float | None:
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, (int, float, str)):
                try:
                    return float(candidate)
                except (TypeError, ValueError, OverflowError):
                    pass
        for child in value.values():
            result = _find_float(child, keys)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_float(child, keys)
            if result is not None:
                return result
    return None


def _find_int(value: object, keys: tuple[str, ...]) -> int | None:
    candidate = _find_float(value, keys)
    return int(candidate) if candidate is not None else None
