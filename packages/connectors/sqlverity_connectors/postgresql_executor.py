from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from threading import RLock
from time import perf_counter
from typing import Any, Protocol, cast

from packages.domain.sqlverity_domain.contracts import ExplainResult, ReadOnlyResult
from packages.domain.sqlverity_domain.models import DataSource, DataSourceCapability

from .connection import PostgreSQLConnectionSecret, SecretResolver


class ReadOnlyExecutorConfigurationError(ValueError):
    pass


class ReadOnlyExecutionError(RuntimeError):
    pass


class ReadOnlyExecutorUnavailableError(RuntimeError):
    pass


class ExecutionCursor(Protocol):
    description: Any

    def execute(self, query: str, parameters: object = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchmany(self, size: int) -> list[Any]: ...

    def __enter__(self) -> ExecutionCursor: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class ExecutionConnection(Protocol):
    def cursor(self) -> ExecutionCursor: ...

    def cancel(self) -> None: ...


ExecutionConnectFactory = Callable[..., AbstractContextManager[ExecutionConnection]]


class PostgreSQLReadOnlyExecutor:
    """PostgreSQL EXPLAIN and bounded SELECT execution with active cancellation."""

    def __init__(
        self,
        secret_resolver: SecretResolver,
        connect_factory: ExecutionConnectFactory | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._connect_factory = connect_factory
        self._active_lock = RLock()
        self._active: dict[str, ExecutionConnection] = {}

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
        started = perf_counter()
        try:
            with self._connect(data_source) as connection:
                self._register(request_id, connection)
                try:
                    with connection.cursor() as cursor:
                        _configure_read_only_transaction(cursor, timeout_seconds)
                        cursor.execute(
                            "EXPLAIN (FORMAT JSON, COSTS TRUE, VERBOSE FALSE, SETTINGS TRUE) "
                            + sql,
                            dict(parameters) if parameters else None,
                        )
                        row = cursor.fetchone()
                finally:
                    self._unregister(request_id, connection)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except Exception:
            raise ReadOnlyExecutionError("PostgreSQL EXPLAIN failed") from None

        plan = _parse_explain_row(row)
        plan_node = plan.get("Plan")
        estimated_cost = _optional_float(plan_node, "Total Cost")
        estimated_rows = _optional_int(plan_node, "Plan Rows")
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
        started = perf_counter()
        try:
            with self._connect(data_source) as connection:
                self._register(request_id, connection)
                try:
                    with connection.cursor() as cursor:
                        _configure_read_only_transaction(cursor, timeout_seconds)
                        cursor.execute(sql, dict(parameters) if parameters else None)
                        columns = _column_names(cursor.description)
                        rows, result_bytes, truncation_reason = _fetch_bounded_rows(
                            cursor,
                            columns,
                            max_rows,
                            max_result_bytes,
                        )
                finally:
                    self._unregister(request_id, connection)
        except (ReadOnlyExecutorConfigurationError, ReadOnlyExecutorUnavailableError):
            raise
        except Exception:
            raise ReadOnlyExecutionError("PostgreSQL read-only execution failed") from None

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
        if data_source.dialect.casefold() not in {"postgres", "postgresql"}:
            raise ReadOnlyExecutorConfigurationError(
                "PostgreSQLReadOnlyExecutor requires PostgreSQL dialect"
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

    def _connect(self, data_source: DataSource) -> AbstractContextManager[ExecutionConnection]:
        secret_ref = data_source.connection_secret_ref
        if secret_ref is None:
            raise ReadOnlyExecutorConfigurationError(
                "DataSource has no connection secret reference"
            )
        secret = self._secret_resolver.resolve_postgresql(secret_ref)
        if self._connect_factory is not None:
            return self._connect_factory(
                **secret.as_connect_kwargs(application_name="sqlverity-read-only-executor")
            )
        return _connect_psycopg(secret)

    def _register(self, request_id: str, connection: ExecutionConnection) -> None:
        with self._active_lock:
            if request_id in self._active:
                raise ReadOnlyExecutionError("Query request is already active")
            self._active[request_id] = connection

    def _unregister(self, request_id: str, connection: ExecutionConnection) -> None:
        with self._active_lock:
            if self._active.get(request_id) is connection:
                del self._active[request_id]


def _connect_psycopg(
    secret: PostgreSQLConnectionSecret,
) -> AbstractContextManager[ExecutionConnection]:
    try:
        import psycopg
    except ImportError:
        raise ReadOnlyExecutorUnavailableError(
            "Install the 'psycopg' project dependency"
        ) from None
    try:
        connection = psycopg.connect(
            host=secret.host,
            port=secret.port,
            dbname=secret.database,
            user=secret.username,
            password=secret.password,
            sslmode=secret.sslmode,
            connect_timeout=secret.connect_timeout_seconds,
            application_name="sqlverity-read-only-executor",
        )
    except Exception:
        raise ReadOnlyExecutorUnavailableError("PostgreSQL connection failed") from None
    return cast(AbstractContextManager[ExecutionConnection], connection)


def _configure_read_only_transaction(
    cursor: ExecutionCursor,
    timeout_seconds: int,
) -> None:
    cursor.execute("SET TRANSACTION READ ONLY")
    cursor.execute(
        "SELECT pg_catalog.set_config('statement_timeout', %s, true)",
        (f"{timeout_seconds * 1_000}ms",),
    )


def _parse_explain_row(row: Any) -> Mapping[str, Any]:
    if not row:
        raise ReadOnlyExecutionError("PostgreSQL EXPLAIN returned no plan")
    raw = row[0]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ReadOnlyExecutionError("PostgreSQL EXPLAIN returned invalid JSON") from None
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], Mapping):
        return dict(raw[0])
    if isinstance(raw, Mapping):
        return dict(raw)
    raise ReadOnlyExecutionError("PostgreSQL EXPLAIN returned an invalid plan")


def _optional_float(value: object, key: str) -> float | None:
    if not isinstance(value, Mapping):
        return None
    candidate = value.get(key)
    if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
        return float(candidate)
    return None


def _optional_int(value: object, key: str) -> int | None:
    if not isinstance(value, Mapping):
        return None
    candidate = value.get(key)
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return candidate
    return None


def _column_names(description: Any) -> tuple[str, ...]:
    if description is None:
        return ()
    return tuple(
        item.name if hasattr(item, "name") else str(item[0])
        for item in description
    )


def _fetch_bounded_rows(
    cursor: ExecutionCursor,
    columns: tuple[str, ...],
    max_rows: int,
    max_result_bytes: int,
) -> tuple[tuple[Mapping[str, Any], ...], int, str | None]:
    rows: list[Mapping[str, Any]] = []
    result_bytes = 0
    while len(rows) < max_rows:
        batch_size = min(50, max_rows - len(rows))
        raw_rows = cursor.fetchmany(batch_size)
        if not raw_rows:
            return tuple(rows), result_bytes, None
        for raw_row in raw_rows:
            row = dict(zip(columns, raw_row, strict=True))
            row_bytes = len(
                json.dumps(
                    row,
                    default=str,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if result_bytes + row_bytes > max_result_bytes:
                return tuple(rows), result_bytes, "result_bytes"
            rows.append(row)
            result_bytes += row_bytes
        if len(raw_rows) < batch_size:
            return tuple(rows), result_bytes, None
    # The validated SQL is already limited, so reaching the boundary is the
    # conservative signal that more source rows may have existed.
    return tuple(rows), result_bytes, "row_limit"


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1_000))
