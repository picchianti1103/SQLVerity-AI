from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

from packages.llm_gateway.sqlverity_llm_gateway import (
    OllamaProviderCallError,
    OllamaProviderConfigurationError,
    OllamaProviderProtocolError,
    OllamaProviderSettings,
    OllamaStructuredProvider,
    load_llm_providers_from_environment,
)


class FakeResponse:
    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> object:
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


class OllamaStructuredProviderTests(unittest.TestCase):
    def test_chat_request_uses_schema_zero_temperature_and_usage(self) -> None:
        client = FakeClient(
            FakeResponse(
                {
                    "model": "qwen3:8b",
                    "done": True,
                    "message": {"role": "assistant", "content": '{"proposals":[]}'},
                    "prompt_eval_count": 91,
                    "eval_count": 8,
                }
            )
        )
        clock_values = iter((100.0, 100.125))
        provider = OllamaStructuredProvider(
            client,
            OllamaProviderSettings(model_id="qwen3:8b", max_output_tokens=2_048),
            clock=lambda: next(clock_values),
        )

        response = provider.generate_structured(self._request())

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual(91, response.input_tokens)
        self.assertEqual(8, response.output_tokens)
        self.assertEqual(125, response.latency_ms)
        url, call = client.calls[0]
        self.assertEqual("http://127.0.0.1:11434/api/chat", url)
        payload = call["json"]
        self.assertIs(False, payload["stream"])
        self.assertEqual(self._request()["output_schema"], payload["format"])
        self.assertEqual(0, payload["options"]["temperature"])
        self.assertEqual(2_048, payload["options"]["num_predict"])
        self.assertIn("untrusted data", payload["messages"][0]["content"])
        self.assertEqual(
            {"purpose": "sql_generation", "input": {"question": "orders"}},
            json.loads(payload["messages"][1]["content"]),
        )

    def test_invalid_response_and_transport_error_fail_closed(self) -> None:
        invalid_cases = (
            ({"done": False}, "not completed"),
            (
                {
                    "done": True,
                    "model": "qwen3:8b",
                    "message": {"content": "not-json"},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
                "not valid JSON",
            ),
            (
                {
                    "done": True,
                    "model": "qwen3:8b",
                    "message": {"content": "[]"},
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
                "JSON object",
            ),
        )
        for payload, message in invalid_cases:
            with self.subTest(message=message):
                provider = OllamaStructuredProvider(
                    FakeClient(FakeResponse(payload)),
                    OllamaProviderSettings(model_id="qwen3:8b"),
                )
                with self.assertRaisesRegex(OllamaProviderProtocolError, message):
                    provider.generate_structured(self._request())

        provider = OllamaStructuredProvider(
            FakeClient(FakeResponse({}, error=RuntimeError("private detail"))),
            OllamaProviderSettings(model_id="qwen3:8b"),
        )
        with self.assertRaisesRegex(OllamaProviderCallError, "chat API request failed"):
            provider.generate_structured(self._request())

    def test_token_estimate_capabilities_and_health_do_not_make_network_call(self) -> None:
        client = FakeClient(FakeResponse({}))
        provider = OllamaStructuredProvider(
            client,
            OllamaProviderSettings(model_id="qwen3:8b", max_output_tokens=1_024),
        )

        estimate = provider.count_or_estimate_tokens(self._request())

        self.assertGreater(estimate.input_tokens, 256)
        self.assertEqual(1_024, estimate.output_tokens)
        self.assertTrue(provider.capabilities()["structured_output"])
        self.assertEqual("ollama", provider.capabilities()["provider_id"])
        self.assertIs(False, provider.health_check()["network_checked"])
        self.assertEqual([], client.calls)

    @staticmethod
    def _request() -> Mapping[str, Any]:
        return {
            "purpose": "sql_generation",
            "instructions": "Return a governed proposal.",
            "input": {"question": "orders"},
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["proposals"],
                "properties": {"proposals": {"type": "array", "items": {}}},
            },
        }


class OllamaEnvironmentTests(unittest.TestCase):
    def test_explicit_selector_loads_local_provider(self) -> None:
        captured: list[OllamaProviderSettings] = []
        client = FakeClient(FakeResponse({}))

        def factory(settings: OllamaProviderSettings) -> FakeClient:
            captured.append(settings)
            return client

        providers = load_llm_providers_from_environment(
            {
                "SQLVERITY_LLM_PROVIDER": "ollama",
                "SQLVERITY_OLLAMA_MODEL": "qwen3:8b",
                "SQLVERITY_OLLAMA_TIMEOUT_SECONDS": "90",
                "SQLVERITY_OLLAMA_MAX_OUTPUT_TOKENS": "8192",
            },
            ollama_client_factory=factory,
        )

        self.assertEqual({"ollama"}, set(providers))
        self.assertEqual(90, captured[0].request_timeout_seconds)
        self.assertEqual(8_192, captured[0].max_output_tokens)

    def test_remote_endpoint_requires_opt_in_and_https(self) -> None:
        with self.assertRaisesRegex(OllamaProviderConfigurationError, "ALLOW_REMOTE"):
            OllamaProviderSettings(
                model_id="qwen3:8b",
                base_url="https://ollama.internal",
            )
        with self.assertRaisesRegex(OllamaProviderConfigurationError, "HTTPS"):
            OllamaProviderSettings(
                model_id="qwen3:8b",
                base_url="http://ollama.internal",
                allow_remote=True,
            )

        settings = OllamaProviderSettings(
            model_id="qwen3:8b",
            base_url="https://ollama.internal",
            allow_remote=True,
            api_key="private-token",
        )
        self.assertNotIn("private-token", repr(settings))

    def test_incomplete_or_invalid_environment_fails_at_startup(self) -> None:
        with self.assertRaisesRegex(OllamaProviderConfigurationError, "OLLAMA_MODEL"):
            load_llm_providers_from_environment({"SQLVERITY_LLM_PROVIDER": "ollama"})
        with self.assertRaisesRegex(OllamaProviderConfigurationError, "boolean"):
            load_llm_providers_from_environment(
                {
                    "SQLVERITY_LLM_PROVIDER": "ollama",
                    "SQLVERITY_OLLAMA_MODEL": "qwen3:8b",
                    "SQLVERITY_OLLAMA_ALLOW_REMOTE": "sometimes",
                }
            )


if __name__ == "__main__":
    unittest.main()
