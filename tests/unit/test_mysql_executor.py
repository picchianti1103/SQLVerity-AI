from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import MySQLConnectionSecret
from packages.connectors.sqlverity_connectors.mysql_executor import MySQLReadOnlyExecutor
from packages.connectors.sqlverity_connectors.postgresql_executor import ReadOnlyExecutionError
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
)


@dataclass(frozen=True)
class Description:
    name: str


class FakeSecretResolver:
    def resolve_mysql(self, secret_ref: str) -> MySQLConnectionSecret:
        return MySQLConnectionSecret(
            host="mysql.internal",
            database="analytics",
            username="reader",
            password="do-not-leak",
        )


class FakeCursor:
    def __init__(
        self,
        *,
        started: threading.Event | None = None,
        released: threading.Event | None = None,
    ) -> None:
        self.description: tuple[Description, ...] = ()
        self.statements: list[tuple[str, object]] = []
        self._one: Any = None
        self._rows = [(1, "first"), (2, "second"), (3, "third")]
        self._started = started
        self._released = released

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append((query, parameters))
        if query.startswith("EXPLAIN FORMAT=JSON"):
            self._one = (
                '{"query_block":{"cost_info":{"query_cost":"42.5"},'
                '"table":{"rows_examined_per_scan":12}}}',
            )
        elif query.startswith("SELECT id"):
            self.description = (Description("id"), Description("label"))
            if self._started is not None and self._released is not None:
                self._started.set()
                self._released.wait(timeout=5)
                raise RuntimeError("cancelled")
        elif query.startswith("KILL QUERY") and self._released is not None:
            self._released.set()

    def fetchone(self) -> Any:
        return self._one

    def fetchmany(self, size: int) -> list[Any]:
        return self._rows[:size]

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeConnection:
    def __init__(self, connection_id: int, cursor: FakeCursor | None = None) -> None:
        self.connection_id = connection_id
        self.cursor_instance = cursor or FakeCursor()

    def cursor(self, **kwargs: Any) -> FakeCursor:
        return self.cursor_instance

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeFactory:
    def __init__(self, connections: list[FakeConnection] | None = None) -> None:
        self.connections = connections or [FakeConnection(77)]
        self.calls = 0

    def __call__(self, **kwargs: Any) -> FakeConnection:
        index = min(self.calls, len(self.connections) - 1)
        self.calls += 1
        return self.connections[index]


def data_source(dialect: str = "mysql") -> DataSource:
    return DataSource(
        tenant_id="tenant-1",
        name="Analytics",
        source_type=DataSourceType.DIRECT_DB,
        dialect=dialect,
        capabilities=frozenset(
            {
                DataSourceCapability.EXPLAIN,
                DataSourceCapability.EXECUTE_READ_ONLY,
                DataSourceCapability.CANCEL,
            }
        ),
        connection_secret_ref="vault://analytics",
    )


class MySQLReadOnlyExecutorTests(unittest.TestCase):
    def test_explain_is_json_read_only_bounded_and_never_analyzes(self) -> None:
        connection = FakeConnection(77)
        executor = MySQLReadOnlyExecutor(FakeSecretResolver(), FakeFactory([connection]))

        result = executor.explain(
            data_source(),
            "request-1",
            "SELECT id FROM analytics.orders LIMIT 10",
            {},
            timeout_seconds=15,
        )

        self.assertEqual(42.5, result.estimated_total_cost)
        self.assertEqual(12, result.estimated_rows)
        statements = connection.cursor_instance.statements
        self.assertEqual((15000,), statements[0][1])
        self.assertEqual("START TRANSACTION READ ONLY", statements[1][0])
        self.assertTrue(statements[2][0].startswith("EXPLAIN FORMAT=JSON"))
        self.assertNotIn("ANALYZE", statements[2][0])

    def test_execution_is_bounded_by_rows(self) -> None:
        executor = MySQLReadOnlyExecutor(FakeSecretResolver(), FakeFactory())

        result = executor.execute_read_only(
            data_source(),
            "request-1",
            "SELECT id FROM analytics.orders LIMIT 2",
            {},
            timeout_seconds=15,
            max_rows=2,
            max_result_bytes=10_000,
        )

        self.assertEqual(2, result.row_count)
        self.assertEqual("row_limit", result.truncation_reason)
        self.assertEqual(("id", "label"), result.columns)

    def test_named_parameters_are_rendered_for_mysql_connector(self) -> None:
        connection = FakeConnection(77)
        executor = MySQLReadOnlyExecutor(
            FakeSecretResolver(),
            FakeFactory([connection]),
        )

        executor.execute_read_only(
            data_source(),
            "request-parameters",
            "SELECT id FROM analytics.orders WHERE id = :order_id LIMIT 2",
            {"order_id": 7},
            timeout_seconds=15,
            max_rows=2,
            max_result_bytes=10_000,
        )

        self.assertIn(
            (
                "SELECT id FROM analytics.orders WHERE id = %(order_id)s LIMIT 2",
                {"order_id": 7},
            ),
            connection.cursor_instance.statements,
        )

    def test_mariadb_uses_max_statement_time_seconds(self) -> None:
        connection = FakeConnection(78)
        executor = MySQLReadOnlyExecutor(
            FakeSecretResolver(),
            FakeFactory([connection]),
            dialect="mariadb",
        )

        executor.explain(
            data_source("mariadb"),
            "request-1",
            "SELECT id FROM analytics.orders LIMIT 10",
            {},
            timeout_seconds=9,
        )

        self.assertEqual(
            "SET SESSION max_statement_time = %s",
            connection.cursor_instance.statements[0][0],
        )
        self.assertEqual((9,), connection.cursor_instance.statements[0][1])

    def test_active_query_is_cancelled_via_separate_kill_query_connection(self) -> None:
        started = threading.Event()
        released = threading.Event()
        main = FakeConnection(77, FakeCursor(started=started, released=released))
        control = FakeConnection(88, FakeCursor(released=released))
        executor = MySQLReadOnlyExecutor(
            FakeSecretResolver(),
            FakeFactory([main, control]),
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                executor.execute_read_only(
                    data_source(),
                    "request-active",
                    "SELECT id FROM analytics.orders LIMIT 1",
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
        self.assertEqual("KILL QUERY 77", control.cursor_instance.statements[0][0])
        self.assertIsInstance(errors[0], ReadOnlyExecutionError)


if __name__ == "__main__":
    unittest.main()
