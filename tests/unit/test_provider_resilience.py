from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any, cast

from packages.llm_gateway.sqlverity_llm_gateway.provider_http import (
    ProviderCircuitOpenError,
    ResilientProviderHTTPClient,
)


class FakeResponse:
    def __init__(self, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return {}


class SequencedHTTPClient:
    def __init__(self, outcomes: tuple[FakeResponse | Exception, ...]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ProviderResilienceTests(unittest.TestCase):
    def test_transient_status_is_retried_but_protocol_success_is_not(self) -> None:
        transport = SequencedHTTPClient((FakeResponse(503), FakeResponse(200)))
        delays: list[float] = []
        client = ResilientProviderHTTPClient(
            transport,
            max_attempts=3,
            retry_base_seconds=0.1,
            sleeper=delays.append,
        )

        response = client.post("https://provider.example/v1")

        self.assertEqual(200, cast(FakeResponse, response).status_code)
        self.assertEqual(2, transport.calls)
        self.assertEqual([0.1], delays)

    def test_retry_after_is_bounded(self) -> None:
        transport = SequencedHTTPClient(
            (FakeResponse(429, {"Retry-After": "60"}), FakeResponse(200))
        )
        delays: list[float] = []
        client = ResilientProviderHTTPClient(transport, sleeper=delays.append)

        client.post("https://provider.example/v1")

        self.assertEqual([5.0], delays)

    def test_circuit_opens_after_bounded_failed_calls_and_recovers(self) -> None:
        now = [10.0]
        transport = SequencedHTTPClient(
            (
                RuntimeError("offline"),
                RuntimeError("offline"),
                FakeResponse(200),
            )
        )
        client = ResilientProviderHTTPClient(
            transport,
            max_attempts=1,
            failure_threshold=2,
            recovery_seconds=30,
            clock=lambda: now[0],
            sleeper=lambda _delay: None,
        )

        with self.assertRaises(RuntimeError):
            client.post("https://provider.example/v1")
        with self.assertRaises(RuntimeError):
            client.post("https://provider.example/v1")
        with self.assertRaises(ProviderCircuitOpenError):
            client.post("https://provider.example/v1")
        self.assertEqual(2, transport.calls)

        now[0] = 41.0
        recovered = cast(FakeResponse, client.post("https://provider.example/v1"))
        self.assertEqual(200, recovered.status_code)


if __name__ == "__main__":
    unittest.main()
