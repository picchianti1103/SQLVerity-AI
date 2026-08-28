from __future__ import annotations

import xml.etree.ElementTree as element_tree
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

from .connection import SQLServerConnectionSecret, SQLServerSecretResolver
from .postgresql_executor import (
    ExecutionCursor,
    ReadOnlyExecutionError,
    ReadOnlyExecutorConfigurationError,
    ReadOnlyExecutorUnavailableError,
    _column_names,
    _elapsed_ms,
    _fetch_bounded_rows,
)
from .sqlserver import _set_read_only

_MAX_PLAN_BYTES = 5_000_000


class SQLServerExecutionCursor(ExecutionCursor, Protocol):
    def __enter__(self) -> SQLServerExecutionCursor: ...


class SQLServerExecutionConnection(Protocol):
    timeout: int

    def cursor(self) -> SQLServerExecutionCursor: ...

    def set_attr(self, attribute: int, value: object) -> None: ...

    def rollback(self) -> None: ...


SQLServerExecutionConnectFactory = Callable[
    ...,
    AbstractContextManager[SQLServerExecutionConnection],
]


@dataclass(frozen=True, slots=True)
class _ActiveQuery:
    secret: SQLServerConnectionSecret
    session_id: int


class SQLServerReadOnlyExecutor:
    """Bounded SQL Server SELECT execution with SHOWPLAN XML and cursor cancellation."""

    def __init__(
        self,
        secret_resolver: SQLServerSecretResolver,
        connect_factory: SQLServerExecutionConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory
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
            parameters,
            timeout_seconds,
            DataSourceCapability.EXPLAIN,
        )
        secret = self._resolve_secret(data_source)
        driver_sql, driver_parameters = _sqlserver_driver_binding(sql, parameters)
        started = perf_counter()
        try:
            with self._connect(secret) as connection:
                _set_read_only(connection)
                connection.timeout = timeout_seconds
                with connection.cursor() as cursor:
                    active = self._register(request_id, cursor, secret)
                    try:
                        cursor.execute("SET SHOWPLAN_XML ON")
                        if driver_parameters:
                            cursor.execute(driver_sql, driver_parameters)
                        else:
                            cursor.execute(driver_sql)
                        row = cursor.fetchone()
                        cursor.execute("SET SHOWPLAN_XML OFF")
                    finally:
                        self._unregister(request_id, active)
                        connection.rollback()
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError("SQL Server SHOWPLAN_XML failed") from None
        plan_xml = _plan_xml(row)
        estimated_cost, estimated_rows = _plan_estimates(plan_xml)
        return ExplainResult(
            plan={"format": "showplan_xml", "xml": plan_xml},
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
            parameters,
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
        driver_sql, driver_parameters = _sqlserver_driver_binding(sql, parameters)
        started = perf_counter()
        try:
            with self._connect(secret) as connection:
                _set_read_only(connection)
                connection.timeout = timeout_seconds
                with connection.cursor() as cursor:
                    active = self._register(request_id, cursor, secret)
                    try:
                        if driver_parameters:
                            cursor.execute(driver_sql, driver_parameters)
                        else:
                            cursor.execute(driver_sql)
                        columns = _column_names(cursor.description)
                        rows, result_bytes, truncation_reason = _fetch_bounded_rows(
                            cursor,
                            columns,
                            max_rows,
                            max_result_bytes,
                        )
                    finally:
                        self._unregister(request_id, active)
                        connection.rollback()
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError("SQL Server read-only execution failed") from None
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
            with self._connect(active.secret) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"KILL {active.session_id}")
        except Exception:
            return False
        return True

    def _validate_request(
        self,
        data_source: DataSource,
        request_id: str,
        sql: str,
        parameters: Mapping[str, Any],
        timeout_seconds: int,
        capability: DataSourceCapability,
    ) -> None:
        if data_source.dialect.casefold() not in {"mssql", "sqlserver", "tsql"}:
            raise ReadOnlyExecutorConfigurationError(
                "SQLServerReadOnlyExecutor requires SQL Server dialect"
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

    def _resolve_secret(self, data_source: DataSource) -> SQLServerConnectionSecret:
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ReadOnlyExecutorConfigurationError(
                "DataSource has no connection secret reference"
            )
        return self._secret_resolver.resolve_sqlserver(secret_ref)

    def _connect(
        self, secret: SQLServerConnectionSecret
    ) -> AbstractContextManager[SQLServerExecutionConnection]:
        if self._connect_factory is not None:
            return self._connect_factory(**secret.as_connect_kwargs())
        return _connect_sqlserver(secret)

    def _register(
        self,
        request_id: str,
        cursor: SQLServerExecutionCursor,
        secret: SQLServerConnectionSecret,
    ) -> _ActiveQuery:
        cursor.execute("SELECT @@SPID")
        row = cursor.fetchone()
        session_id = row[0] if row else None
        if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
            raise ReadOnlyExecutionError(
                "SQL Server connection did not expose a valid session id"
            )
        active = _ActiveQuery(secret=secret, session_id=session_id)
        with self._active_lock:
            if request_id in self._active:
                raise ReadOnlyExecutionError("Query request is already active")
            self._active[request_id] = active
        return active

    def _unregister(self, request_id: str, active: _ActiveQuery) -> None:
        with self._active_lock:
            if self._active.get(request_id) == active:
                del self._active[request_id]


def _sqlserver_driver_binding(
    sql: str,
    parameters: Mapping[str, Any],
) -> tuple[str, tuple[Any, ...]]:
    if not parameters:
        return sql, ()
    try:
        statement = sqlglot.parse_one(sql, read="tsql")
    except ParseError:
        raise ReadOnlyExecutorConfigurationError(
            "SQL Server parameterized SQL could not be parsed"
        ) from None
    ordered_names: list[str] = []

    def replace_placeholder(node: exp.Expr) -> exp.Expr:
        if not isinstance(node, exp.Placeholder) or node.name == "?":
            return node
        ordered_names.append(node.name)
        return exp.Var(this="?")

    driver_statement = statement.transform(replace_placeholder)
    if set(ordered_names) != set(parameters):
        raise ReadOnlyExecutorConfigurationError(
            "SQL Server placeholders do not match supplied parameters"
        )
    return (
        driver_statement.sql(dialect="tsql"),
        tuple(parameters[name] for name in ordered_names),
    )


def _connect_sqlserver(
    secret: SQLServerConnectionSecret,
) -> AbstractContextManager[SQLServerExecutionConnection]:
    try:
        module = import_module("mssql_python")
        connection = module.connect(**secret.as_connect_kwargs())
    except ImportError:
        raise ReadOnlyExecutorUnavailableError(
            "Install the 'mssql-python' project dependency"
        ) from None
    except Exception:
        raise ReadOnlyExecutorUnavailableError("SQL Server connection failed") from None
    return cast(AbstractContextManager[SQLServerExecutionConnection], connection)


def _plan_xml(row: Any) -> str:
    if not row or not row[0]:
        raise ReadOnlyExecutionError("SQL Server SHOWPLAN_XML returned no plan")
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raw = str(raw)
    if len(raw.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise ReadOnlyExecutionError("SQL Server SHOWPLAN_XML exceeded the plan size limit")
    try:
        element_tree.fromstring(raw)
    except element_tree.ParseError:
        raise ReadOnlyExecutionError("SQL Server SHOWPLAN_XML returned invalid XML") from None
    return raw


def _plan_estimates(plan_xml: str) -> tuple[float | None, int | None]:
    root = element_tree.fromstring(plan_xml)
    statement = next(
        (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "StmtSimple"),
        None,
    )
    if statement is None:
        return None, None
    return (
        _optional_float(statement.attrib.get("StatementSubTreeCost")),
        _optional_int(statement.attrib.get("StatementEstRows")),
    )


def _optional_float(value: object) -> float | None:
    if value is not None and not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_int(value: object) -> int | None:
    candidate = _optional_float(value)
    return max(0, round(candidate)) if candidate is not None else None
