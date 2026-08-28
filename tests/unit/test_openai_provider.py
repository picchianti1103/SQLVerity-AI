from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import httpx
from openai import OpenAI

from packages.llm_gateway.sqlverity_llm_gateway import (
    OpenAIProviderCallError,
    OpenAIProviderConfigurationError,
    OpenAIProviderProtocolError,
    OpenAIProviderSettings,
    OpenAIResponsesProvider,
    load_llm_providers_from_environment,
)


class FakeResponsesResource:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[Mapping[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeOpenAIClient:
    def __init__(self, responses: FakeResponsesResource) -> None:
        self.responses = responses


class OpenAIResponsesProviderTests(unittest.TestCase):
    def test_official_sdk_contract_uses_responses_endpoint(self) -> None:
        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1_786_838_400,
                    "status": "completed",
                    "model": "gpt-test-snapshot",
                    "output": [
                        {
                            "id": "msg_test",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"proposals":[]}',
                                    "annotations": [],
                                    "logprobs": [],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 24,
                        "output_tokens": 4,
                        "total_tokens": 28,
                        "input_tokens_details": {"cached_tokens": 6},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
            )

        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            provider = OpenAIResponsesProvider(
                cast(
                    Any,
                    OpenAI(
                        api_key="test-key",
                        base_url="https://api.openai.com/v1",
                        http_client=http_client,
                        max_retries=0,
                    ),
                ),
                self._settings(),
            )

            response = provider.generate_structured(self._request())
        finally:
            http_client.close()

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual("gpt-test-snapshot", response.model_id)
        self.assertEqual(6, response.cached_input_tokens)
        self.assertEqual(1, len(captured_requests))
        self.assertEqual("/v1/responses", captured_requests[0].url.path)
        sent_payload = json.loads(captured_requests[0].content)
        self.assertIs(False, sent_payload["store"])
        self.assertEqual("json_schema", sent_payload["text"]["format"]["type"])
        self.assertIs(True, sent_payload["text"]["format"]["strict"])

    def test_structured_call_is_stateless_strict_and_records_usage(self) -> None:
        resource = FakeResponsesResource(
            SimpleNamespace(
                status="completed",
                model="gpt-test-2026-08-16",
                output_text='{"proposals":[]}',
                usage=SimpleNamespace(
                    input_tokens=120,
                    output_tokens=17,
                    input_tokens_details=SimpleNamespace(cached_tokens=35),
                ),
            )
        )
        clock_values = iter((100.0, 100.125))
        provider = OpenAIResponsesProvider(
            FakeOpenAIClient(resource),
            self._settings(),
            clock=lambda: next(clock_values),
        )

        response = provider.generate_structured(self._request())

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual("gpt-test-2026-08-16", response.model_id)
        self.assertEqual(120, response.input_tokens)
        self.assertEqual(35, response.cached_input_tokens)
        self.assertEqual(17, response.output_tokens)
        self.assertEqual(125, response.latency_ms)
        self.assertEqual(1, len(resource.calls))
        call = resource.calls[0]
        self.assertEqual("gpt-test", call["model"])
        self.assertIs(False, call["store"])
        self.assertEqual(2_048, call["max_output_tokens"])
        self.assertIn("untrusted data", call["instructions"])
        self.assertEqual(
            {
                "input": {
                    "items": [
                        {
                            "data": {"physical_type": "bigint"},
                            "id": "public.orders.id",
                            "kind": "schema_column",
                        }
                    ],
                    "trust_level": "untrusted_data",
                },
                "purpose": "semantic_description_inference",
            },
            json.loads(call["input"]),
        )
        text = call["text"]
        self.assertEqual("json_schema", text["format"]["type"])
        self.assertIs(True, text["format"]["strict"])
        self.assertEqual(
            "sqlverity_semantic_description_inference",
            text["format"]["name"],
        )
        self.assertEqual(self._request()["output_schema"], text["format"]["schema"])

    def test_token_estimate_is_conservative_and_declares_capabilities(self) -> None:
        provider = OpenAIResponsesProvider(
            FakeOpenAIClient(FakeResponsesResource()),
            self._settings(),
        )

        estimate = provider.count_or_estimate_tokens(self._request())

        self.assertGreater(estimate.input_tokens, 256)
        self.assertEqual(2_048, estimate.output_tokens)
        self.assertEqual(0, estimate.cached_input_tokens)
        self.assertEqual(
            {
                "provider_id": "openai",
                "model_id": "gpt-test",
                "api": "responses",
                "structured_output": True,
                "response_storage": False,
            },
            provider.capabilities(),
        )
        self.assertEqual("configured", provider.health_check()["status"])
        self.assertIs(False, provider.health_check()["network_checked"])

    def test_invalid_or_incomplete_responses_fail_closed(self) -> None:
        cases = (
            (
                SimpleNamespace(
                    status="incomplete",
                    model="gpt-test",
                    output_text='{"proposals":[]}',
                    usage=self._usage(),
                ),
                "not completed",
            ),
            (
                SimpleNamespace(
                    status="completed",
                    model="gpt-test",
                    output_text="not-json",
                    usage=self._usage(),
                ),
                "not valid JSON",
            ),
            (
                SimpleNamespace(
                    status="completed",
                    model="gpt-test",
                    output_text="[]",
                    usage=self._usage(),
                ),
                "JSON object",
            ),
            (
                SimpleNamespace(
                    status="completed",
                    model="gpt-test",
                    output_text='{"proposals":[]}',
                    usage=None,
                ),
                "no usage telemetry",
            ),
        )
        for raw_response, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                provider = OpenAIResponsesProvider(
                    FakeOpenAIClient(FakeResponsesResource(raw_response)),
                    self._settings(),
                )
                with self.assertRaisesRegex(OpenAIProviderProtocolError, expected_message):
                    provider.generate_structured(self._request())

    def test_sdk_failures_are_normalized_without_echoing_credentials(self) -> None:
        provider = OpenAIResponsesProvider(
            FakeOpenAIClient(FakeResponsesResource(error=RuntimeError("transport failed"))),
            self._settings(api_key="secret-key-that-must-not-be-echoed"),
        )

        with self.assertRaises(OpenAIProviderCallError) as raised:
            provider.generate_structured(self._request())

        self.assertEqual("OpenAI Responses API request failed", str(raised.exception))
        self.assertNotIn("secret-key", str(raised.exception))

    def test_request_requires_an_object_output_schema(self) -> None:
        provider = OpenAIResponsesProvider(
            FakeOpenAIClient(FakeResponsesResource()),
            self._settings(),
        )
        request = dict(self._request())
        request["output_schema"] = {"type": "array"}

        with self.assertRaisesRegex(OpenAIProviderProtocolError, "JSON object"):
            provider.count_or_estimate_tokens(request)

    def test_settings_hide_api_key_and_validate_bounds(self) -> None:
        settings = self._settings(api_key="private-api-key")
        self.assertNotIn("private-api-key", repr(settings))
        with self.assertRaisesRegex(
            OpenAIProviderConfigurationError,
            "MAX_OUTPUT_TOKENS",
        ):
            OpenAIProviderSettings(
                api_key="key",
                model_id="model",
                max_output_tokens=0,
            )

    @staticmethod
    def _settings(api_key: str = "test-key") -> OpenAIProviderSettings:
        return OpenAIProviderSettings(
            api_key=api_key,
            model_id="gpt-test",
            max_output_tokens=2_048,
        )

    @staticmethod
    def _usage() -> SimpleNamespace:
        return SimpleNamespace(
            input_tokens=12,
            output_tokens=3,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        )

    @staticmethod
    def _request() -> Mapping[str, Any]:
        return {
            "purpose": "semantic_description_inference",
            "instructions": "Return governed metadata.",
            "input": {
                "trust_level": "untrusted_data",
                "items": [
                    {
                        "id": "public.orders.id",
                        "kind": "schema_column",
                        "data": {"physical_type": "bigint"},
                    }
                ],
            },
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["proposals"],
                "properties": {"proposals": {"type": "array", "items": {}}},
            },
        }


class OpenAIProviderEnvironmentTests(unittest.TestCase):
    def test_provider_is_disabled_without_explicit_selector(self) -> None:
        factory_called = False

        def factory(_settings: OpenAIProviderSettings) -> FakeOpenAIClient:
            nonlocal factory_called
            factory_called = True
            return FakeOpenAIClient(FakeResponsesResource())

        providers = load_llm_providers_from_environment(
            {
                "OPENAI_API_KEY": "present-but-not-enabled",
                "SQLVERITY_OPENAI_MODEL": "gpt-test",
            },
            client_factory=factory,
        )

        self.assertEqual({}, providers)
        self.assertIs(False, factory_called)

    def test_explicit_selector_loads_provider(self) -> None:
        captured_settings: list[OpenAIProviderSettings] = []

        def factory(settings: OpenAIProviderSettings) -> FakeOpenAIClient:
            captured_settings.append(settings)
            return FakeOpenAIClient(FakeResponsesResource())

        providers = load_llm_providers_from_environment(
            {
                "SQLVERITY_LLM_PROVIDER": " OpenAI ",
                "OPENAI_API_KEY": "test-key",
                "SQLVERITY_OPENAI_MODEL": "gpt-test",
                "SQLVERITY_OPENAI_TIMEOUT_SECONDS": "45.5",
                "SQLVERITY_OPENAI_MAX_RETRIES": "3",
                "SQLVERITY_OPENAI_MAX_OUTPUT_TOKENS": "8192",
            },
            client_factory=factory,
        )

        self.assertEqual({"openai"}, set(providers))
        self.assertEqual(1, len(captured_settings))
        self.assertEqual(45.5, captured_settings[0].request_timeout_seconds)
        self.assertEqual(3, captured_settings[0].max_retries)
        self.assertEqual(8_192, captured_settings[0].max_output_tokens)

    def test_unknown_or_incomplete_configuration_fails_at_startup(self) -> None:
        cases = (
            ({"SQLVERITY_LLM_PROVIDER": "unknown"}, "Unsupported"),
            (
                {
                    "SQLVERITY_LLM_PROVIDER": "openai",
                    "SQLVERITY_OPENAI_MODEL": "gpt-test",
                },
                "OPENAI_API_KEY",
            ),
            (
                {
                    "SQLVERITY_LLM_PROVIDER": "openai",
                    "OPENAI_API_KEY": "test-key",
                },
                "SQLVERITY_OPENAI_MODEL",
            ),
            (
                {
                    "SQLVERITY_LLM_PROVIDER": "openai",
                    "OPENAI_API_KEY": "test-key",
                    "SQLVERITY_OPENAI_MODEL": "gpt-test",
                    "SQLVERITY_OPENAI_TIMEOUT_SECONDS": "invalid",
                },
                "TIMEOUT_SECONDS",
            ),
        )
        for environment, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(
                    OpenAIProviderConfigurationError,
                    expected_message,
                ):
                    load_llm_providers_from_environment(environment)


if __name__ == "__main__":
    unittest.main()
