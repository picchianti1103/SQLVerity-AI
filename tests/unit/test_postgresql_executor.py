from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import PostgreSQLConnectionSecret
from packages.connectors.sqlverity_connectors.postgresql_executor import (
    PostgreSQLReadOnlyExecutor,
    ReadOnlyExecutionError,
    ReadOnlyExecutorConfigurationError,
)
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
)


@dataclass(frozen=True)
class Description:
    name: str


class FakeSecretResolver:
    def resolve_postgresql(self, secret_ref: str) -> PostgreSQLConnectionSecret:
        return PostgreSQLConnectionSecret(
            host="db.internal",
            database="analytics",
            username="reader",
            password="do-not-leak",
        )


class FakeExecutionCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.description: tuple[Description, ...] = ()
        self.rows = rows if rows is not None else [(1, "first"), (2, "second"), (3, "third")]
        self.statements: list[tuple[str, object]] = []
        self._one: Any = None

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append((query, parameters))
        if query.startswith("EXPLAIN"):
            self._one = (
                [
                    {
                        "Plan": {
                            "Node Type": "Seq Scan",
                            "Total Cost": 42.5,
                            "Plan Rows": 12,
                        }
                    }
                ],
            )
        elif query.startswith("SELECT id"):
            self.description = (Description("id"), Description("label"))

    def fetchone(self) -> Any:
        return self._one

    def fetchmany(self, size: int) -> list[Any]:
        return self.rows[:size]

    def __enter__(self) -> FakeExecutionCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeExecutionConnection:
    def __init__(self, cursor: FakeExecutionCursor | None = None) -> None:
        self.cursor_instance = cursor if cursor is not None else FakeExecutionCursor()
        self.cancelled = False

    def cursor(self) -> FakeExecutionCursor:
        return self.cursor_instance

    def cancel(self) -> None:
        self.cancelled = True

    def __enter__(self) -> FakeExecutionConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeExecutionConnectFactory:
    def __init__(self, connection: FakeExecutionConnection | None = None) -> None:
        self.connection = connection if connection is not None else FakeExecutionConnection()
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, **kwargs: Any) -> FakeExecutionConnection:
        self.kwargs = kwargs
        return self.connection


def executable_data_source() -> DataSource:
    return DataSource(
        tenant_id="tenant-1",
        name="Analytics",
        source_type=DataSourceType.DIRECT_DB,
        dialect="postgresql",
        capabilities=frozenset(
            {
                DataSourceCapability.EXPLAIN,
                DataSourceCapability.EXECUTE_READ_ONLY,
                DataSourceCapability.CANCEL,
            }
        ),
        connection_secret_ref="vault://analytics",
    )


class PostgreSQLReadOnlyExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = FakeExecutionConnectFactory()
        self.executor = PostgreSQLReadOnlyExecutor(FakeSecretResolver(), self.factory)

    def test_explain_uses_read_only_transaction_timeout_and_no_analyze(self) -> None:
        result = self.executor.explain(
            executable_data_source(),
            "request-1",
            "SELECT id FROM public.orders LIMIT 10",
            {},
            timeout_seconds=15,
        )

        self.assertEqual(42.5, result.estimated_total_cost)
        self.assertEqual(12, result.estimated_rows)
        statements = self.factory.connection.cursor_instance.statements
        self.assertEqual("SET TRANSACTION READ ONLY", statements[0][0])
        self.assertEqual(("15000ms",), statements[1][1])
        self.assertTrue(statements[2][0].startswith("EXPLAIN (FORMAT JSON"))
        self.assertNotIn("ANALYZE", statements[2][0])
        assert self.factory.kwargs is not None
        self.assertEqual("sqlverity-read-only-executor", self.factory.kwargs["application_name"])

    def test_execution_enforces_row_limit_and_returns_metadata(self) -> None:
        result = self.executor.execute_read_only(
            executable_data_source(),
            "request-1",
            "SELECT id FROM public.orders LIMIT 2",
            {},
            timeout_seconds=15,
            max_rows=2,
            max_result_bytes=10_000,
        )

        self.assertEqual(("id", "label"), result.columns)
        self.assertEqual(2, result.row_count)
        self.assertEqual(1, result.rows[0]["id"])
        self.assertTrue(result.truncated)
        self.assertEqual("row_limit", result.truncation_reason)

    def test_execution_stops_serializing_at_byte_limit(self) -> None:
        cursor = FakeExecutionCursor(rows=[(1, "x" * 2_000)])
        executor = PostgreSQLReadOnlyExecutor(
            FakeSecretResolver(),
            FakeExecutionConnectFactory(FakeExecutionConnection(cursor)),
        )

        result = executor.execute_read_only(
            executable_data_source(),
            "request-1",
            "SELECT id FROM public.orders LIMIT 1",
            {},
            timeout_seconds=15,
            max_rows=1,
            max_result_bytes=1_024,
        )

        self.assertEqual(0, result.row_count)
        self.assertTrue(result.truncated)
        self.assertEqual("result_bytes", result.truncation_reason)

    def test_missing_capability_fails_before_connection(self) -> None:
        data_source = DataSource(
            tenant_id="tenant-1",
            name="Offline",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
            connection_secret_ref="vault://analytics",
        )

        with self.assertRaises(ReadOnlyExecutorConfigurationError):
            self.executor.execute_read_only(
                data_source,
                "request-1",
                "SELECT id FROM public.orders LIMIT 1",
                {},
                timeout_seconds=15,
                max_rows=1,
                max_result_bytes=1_024,
            )

    def test_connection_failure_does_not_leak_secret(self) -> None:
        def failing_factory(**kwargs: Any) -> FakeExecutionConnection:
            raise RuntimeError(f"connection failed with {kwargs['password']}")

        executor = PostgreSQLReadOnlyExecutor(FakeSecretResolver(), failing_factory)

        with self.assertRaises(ReadOnlyExecutionError) as raised:
            executor.explain(
                executable_data_source(),
                "request-1",
                "SELECT id FROM public.orders LIMIT 1",
                {},
                timeout_seconds=15,
            )

        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_active_connection_can_be_cancelled(self) -> None:
        started = threading.Event()
        released = threading.Event()

        class BlockingCursor(FakeExecutionCursor):
            def execute(self, query: str, parameters: object = None) -> None:
                super().execute(query, parameters)
                if query.startswith("SELECT id"):
                    started.set()
                    released.wait(timeout=5)
                    raise RuntimeError("cancelled")

        class CancellableConnection(FakeExecutionConnection):
            def cancel(self) -> None:
                super().cancel()
                released.set()

        connection = CancellableConnection(BlockingCursor())
        executor = PostgreSQLReadOnlyExecutor(
            FakeSecretResolver(),
            FakeExecutionConnectFactory(connection),
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                executor.execute_read_only(
                    executable_data_source(),
                    "request-active",
                    "SELECT id FROM public.orders LIMIT 1",
                    {},
                    timeout_seconds=15,
                    max_rows=1,
                    max_result_bytes=1_024,
                )
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(timeout=5))
        self.assertTrue(executor.cancel("request-active"))
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertTrue(connection.cancelled)
        self.assertIsInstance(errors[0], ReadOnlyExecutionError)


if __name__ == "__main__":
    unittest.main()
