from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from typing import Any

from packages.connectors.sqlverity_connectors.connection import (
    OracleConnectionSecret,
    SQLServerConnectionSecret,
)
from packages.connectors.sqlverity_connectors.oracle_executor import OracleReadOnlyExecutor
from packages.connectors.sqlverity_connectors.postgresql_executor import (
    ReadOnlyExecutionError,
)
from packages.connectors.sqlverity_connectors.sqlserver_executor import (
    SQLServerReadOnlyExecutor,
)
from packages.domain.sqlverity_domain.models import (
    DataSource,
    DataSourceCapability,
    DataSourceType,
)


@dataclass(frozen=True)
class Description:
    name: str


SHOWPLAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">
  <BatchSequence><Batch><Statements>
    <StmtSimple StatementEstRows="12" StatementSubTreeCost="42.5" />
  </Statements></Batch></BatchSequence>
</ShowPlanXML>
"""


class FakeCursor:
    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.description: tuple[Description, ...] = ()
        self.statements: list[tuple[str, object]] = []
        self.cancelled = False
        self._one: Any = None
        self._all: list[tuple[Any, ...]] = []
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append((query, parameters))
        if query == "SELECT @@SPID":
            self._one = (77,)
            return
        elif query.startswith("EXPLAIN PLAN"):
            return
        if "DBMS_XPLAN.DISPLAY" in query:
            self._all = [("Plan hash value: 42",), ("TABLE ACCESS FULL ORDERS",)]
        elif query == "SET TRANSACTION READ ONLY":
            return
        elif query == "SET SHOWPLAN_XML ON" or query == "SET SHOWPLAN_XML OFF":
            return
        elif self.dialect == "sqlserver" and query.startswith("SELECT"):
            self._one = (SHOWPLAN_XML,)
        elif query.startswith("SELECT"):
            self.description = (Description("id"), Description("label"))
            self._rows = [(1, "first"), (2, "second"), (3, "third")]

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._all

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def cancel(self) -> None:
        self.cancelled = True

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeConnection:
    def __init__(self, dialect: str, cursor: FakeCursor | None = None) -> None:
        self.cursor_instance = cursor or FakeCursor(dialect)
        self.call_timeout = 0
        self.timeout = 0
        self.attributes: list[tuple[int, object]] = []
        self.cursor_timeouts: list[int | None] = []
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        self.cursor_timeouts.append(self.timeout)
        return self.cursor_instance

    def cancel(self) -> None:
        self.cursor_instance.cancelled = True

    def set_attr(self, attribute: int, value: object) -> None:
        self.attributes.append((attribute, value))

    def rollback(self) -> None:
        self.rollback_count += 1

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None


class FakeFactory:
    def __init__(
        self,
        dialect: str,
        connections: list[FakeConnection] | None = None,
    ) -> None:
        self.connections = connections or [FakeConnection(dialect)]
        self.connection = self.connections[0]
        self.calls = 0

    def __call__(self, **kwargs: Any) -> FakeConnection:
        index = min(self.calls, len(self.connections) - 1)
        self.calls += 1
        return self.connections[index]


class BlockingSQLServerCursor(FakeCursor):
    def __init__(self, started: threading.Event, released: threading.Event) -> None:
        super().__init__("sqlserver")
        self._started = started
        self._released = released

    def execute(self, query: str, parameters: object = None) -> None:
        if query.startswith("SELECT TOP"):
            self.statements.append((query, parameters))
            self._started.set()
            self._released.wait(timeout=5)
            raise RuntimeError("cancelled")
        super().execute(query, parameters)


class ControlSQLServerCursor(FakeCursor):
    def __init__(self, released: threading.Event) -> None:
        super().__init__("sqlserver")
        self._released = released

    def execute(self, query: str, parameters: object = None) -> None:
        self.statements.append((query, parameters))
        if query.startswith("KILL "):
            self._released.set()


class OracleResolver:
    def resolve_oracle(self, secret_ref: str) -> OracleConnectionSecret:
        return OracleConnectionSecret(
            host="oracle.internal",
            service_name="analytics",
            username="reader",
            password="secret",
        )


class SQLServerResolver:
    def resolve_sqlserver(self, secret_ref: str) -> SQLServerConnectionSecret:
        return SQLServerConnectionSecret(
            host="sqlserver.internal",
            database="analytics",
            username="reader",
            password="secret",
        )


def data_source(dialect: str) -> DataSource:
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


class OracleExecutorTests(unittest.TestCase):
    def test_explain_uses_dbms_xplan_and_rolls_back_plan_table_write(self) -> None:
        factory = FakeFactory("oracle")
        result = OracleReadOnlyExecutor(OracleResolver(), factory).explain(
            data_source("oracle"),
            "request-1",
            "SELECT ID FROM SALES.ORDERS FETCH FIRST 10 ROWS ONLY",
            {},
            timeout_seconds=15,
        )

        self.assertEqual("dbms_xplan_text", result.plan["format"])
        self.assertEqual(15_000, factory.connection.call_timeout)
        self.assertTrue(
            factory.connection.cursor_instance.statements[0][0].startswith("EXPLAIN PLAN")
        )
        self.assertIn("DBMS_XPLAN.DISPLAY", factory.connection.cursor_instance.statements[1][0])
        self.assertEqual(1, factory.connection.rollback_count)

    def test_read_only_execution_is_bounded(self) -> None:
        factory = FakeFactory("oracle")
        result = OracleReadOnlyExecutor(OracleResolver(), factory).execute_read_only(
            data_source("oracle"),
            "request-1",
            "SELECT ID, LABEL FROM SALES.ORDERS FETCH FIRST 2 ROWS ONLY",
            {},
            timeout_seconds=10,
            max_rows=2,
            max_result_bytes=10_000,
        )

        self.assertEqual(2, result.row_count)
        self.assertEqual("row_limit", result.truncation_reason)
        self.assertEqual(
            "SET TRANSACTION READ ONLY",
            factory.connection.cursor_instance.statements[0][0],
        )

    def test_named_parameters_remain_native_oracle_bindings(self) -> None:
        factory = FakeFactory("oracle")
        executor = OracleReadOnlyExecutor(OracleResolver(), factory)

        executor.execute_read_only(
            data_source("oracle"),
            "request-parameters",
            "SELECT id FROM orders WHERE id = :order_id FETCH FIRST 2 ROWS ONLY",
            {"order_id": 7},
            timeout_seconds=12,
            max_rows=2,
            max_result_bytes=1_024,
        )

        self.assertIn(
            (
                "SELECT id FROM orders WHERE id = :order_id FETCH FIRST 2 ROWS ONLY",
                {"order_id": 7},
            ),
            factory.connection.cursor_instance.statements,
        )


class SQLServerExecutorTests(unittest.TestCase):
    def test_explain_uses_showplan_xml_without_executing_query(self) -> None:
        factory = FakeFactory("sqlserver")
        result = SQLServerReadOnlyExecutor(SQLServerResolver(), factory).explain(
            data_source("mssql"),
            "request-1",
            "SELECT TOP 10 id FROM sales.orders",
            {},
            timeout_seconds=12,
        )

        self.assertEqual("showplan_xml", result.plan["format"])
        self.assertEqual(42.5, result.estimated_total_cost)
        self.assertEqual(12, result.estimated_rows)
        self.assertEqual(12, factory.connection.cursor_timeouts[0])
        self.assertEqual([(101, 1)], factory.connection.attributes)
        self.assertEqual("SELECT @@SPID", factory.connection.cursor_instance.statements[0][0])
        self.assertEqual("SET SHOWPLAN_XML ON", factory.connection.cursor_instance.statements[1][0])
        self.assertEqual(
            "SET SHOWPLAN_XML OFF",
            factory.connection.cursor_instance.statements[3][0],
        )

    def test_governed_named_parameters_are_converted_to_positional_bindings(self) -> None:
        factory = FakeFactory("sqlserver")
        executor = SQLServerReadOnlyExecutor(SQLServerResolver(), factory)

        executor.execute_read_only(
            data_source("sqlserver"),
            "request-1",
            "SELECT TOP 1 id FROM sales.orders WHERE id = :id",
            {"id": 1},
            timeout_seconds=12,
            max_rows=1,
            max_result_bytes=1_024,
        )

        self.assertIn(
            ("SELECT TOP 1 id FROM sales.orders WHERE id = ?", (1,)),
            factory.connection.cursor_instance.statements,
        )

    def test_active_query_is_cancelled_with_validated_session_id(self) -> None:
        started = threading.Event()
        released = threading.Event()
        main = FakeConnection(
            "sqlserver",
            BlockingSQLServerCursor(started, released),
        )
        control = FakeConnection("sqlserver", ControlSQLServerCursor(released))
        executor = SQLServerReadOnlyExecutor(
            SQLServerResolver(),
            FakeFactory("sqlserver", [main, control]),
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                executor.execute_read_only(
                    data_source("sqlserver"),
                    "request-active",
                    "SELECT TOP 1 id FROM sales.orders",
                    {},
                    timeout_seconds=12,
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
        self.assertEqual("KILL 77", control.cursor_instance.statements[0][0])
        self.assertIsInstance(errors[0], ReadOnlyExecutionError)


if __name__ == "__main__":
    unittest.main()
