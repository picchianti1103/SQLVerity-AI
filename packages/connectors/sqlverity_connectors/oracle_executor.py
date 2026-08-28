from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from importlib import import_module
from threading import RLock
from time import perf_counter
from typing import Any, Protocol, cast

from packages.domain.sqlverity_domain.contracts import ExplainResult, ReadOnlyResult
from packages.domain.sqlverity_domain.models import DataSource, DataSourceCapability

from .connection import OracleConnectionSecret, OracleSecretResolver
from .postgresql_executor import (
    ExecutionCursor,
    ReadOnlyExecutionError,
    ReadOnlyExecutorConfigurationError,
    ReadOnlyExecutorUnavailableError,
    _column_names,
    _elapsed_ms,
    _fetch_bounded_rows,
)


class OracleExecutionCursor(ExecutionCursor, Protocol):
    def fetchall(self) -> list[Any]: ...

    def __enter__(self) -> OracleExecutionCursor: ...


class OracleExecutionConnection(Protocol):
    call_timeout: int

    def cursor(self) -> OracleExecutionCursor: ...

    def cancel(self) -> None: ...

    def rollback(self) -> None: ...


OracleExecutionConnectFactory = Callable[
    ...,
    AbstractContextManager[OracleExecutionConnection],
]


class OracleReadOnlyExecutor:
    """Bounded Oracle SELECT execution and DBMS_XPLAN-based estimated plans."""

    def __init__(
        self,
        secret_resolver: OracleSecretResolver,
        connect_factory: OracleExecutionConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory
        self._active_lock = RLock()
        self._active: dict[str, OracleExecutionConnection] = {}

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
        statement_id = f"SQLVERITY_{uuid.uuid4().hex[:24].upper()}"
        started = perf_counter()
        try:
            with self._connect(data_source) as connection:
                connection.call_timeout = timeout_seconds * 1_000
                self._register(request_id, connection)
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}",
                            dict(parameters) if parameters else None,
                        )
                        cursor.execute(
                            "SELECT PLAN_TABLE_OUTPUT FROM "
                            "TABLE(DBMS_XPLAN.DISPLAY(NULL, :statement_id, 'TYPICAL'))",
                            {"statement_id": statement_id},
                        )
                        lines = tuple(str(row[0]) for row in cursor.fetchall())
                finally:
                    connection.rollback()
                    self._unregister(request_id, connection)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError("Oracle EXPLAIN PLAN failed") from None
        if not lines:
            raise ReadOnlyExecutionError("Oracle EXPLAIN PLAN returned no plan")
        return ExplainResult(
            plan={"format": "dbms_xplan_text", "lines": lines},
            estimated_total_cost=None,
            estimated_rows=None,
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
        started = perf_counter()
        try:
            with self._connect(data_source) as connection:
                connection.call_timeout = timeout_seconds * 1_000
                self._register(request_id, connection)
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION READ ONLY")
                        cursor.execute(sql, dict(parameters) if parameters else None)
                        columns = _column_names(cursor.description)
                        rows, result_bytes, truncation_reason = _fetch_bounded_rows(
                            cursor,
                            columns,
                            max_rows,
                            max_result_bytes,
                        )
                finally:
                    connection.rollback()
                    self._unregister(request_id, connection)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except ReadOnlyExecutionError:
            raise
        except Exception:
            raise ReadOnlyExecutionError("Oracle read-only execution failed") from None
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
            connection = self._active.get(request_id)
        if connection is None:
            return False
        try:
            connection.cancel()
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
        if data_source.dialect.casefold() != "oracle":
            raise ReadOnlyExecutorConfigurationError(
                "OracleReadOnlyExecutor requires Oracle dialect"
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

    def _connect(
        self, data_source: DataSource
    ) -> AbstractContextManager[OracleExecutionConnection]:
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ReadOnlyExecutorConfigurationError(
                "DataSource has no connection secret reference"
            )
        secret = self._secret_resolver.resolve_oracle(secret_ref)
        if self._connect_factory is not None:
            return self._connect_factory(**secret.as_connect_kwargs())
        return _connect_oracle(secret)

    def _register(self, request_id: str, connection: OracleExecutionConnection) -> None:
        with self._active_lock:
            if request_id in self._active:
                raise ReadOnlyExecutionError("Query request is already active")
            self._active[request_id] = connection

    def _unregister(self, request_id: str, connection: OracleExecutionConnection) -> None:
        with self._active_lock:
            if self._active.get(request_id) is connection:
                del self._active[request_id]


def _connect_oracle(
    secret: OracleConnectionSecret,
) -> AbstractContextManager[OracleExecutionConnection]:
    try:
        module = import_module("oracledb")
        connection = module.connect(**secret.as_connect_kwargs())
    except ImportError:
        raise ReadOnlyExecutorUnavailableError(
            "Install the 'oracledb' project dependency"
        ) from None
    except Exception:
        raise ReadOnlyExecutorUnavailableError("Oracle connection failed") from None
    return cast(AbstractContextManager[OracleExecutionConnection], connection)
