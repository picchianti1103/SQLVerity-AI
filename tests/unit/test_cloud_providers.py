from __future__ import annotations

import json
import unittest
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from packages.domain.sqlverity_domain.contracts import LLMResponse, TokenEstimate
from packages.llm_gateway.sqlverity_llm_gateway import (
    AnthropicProviderCallError,
    AnthropicProviderConfigurationError,
    AnthropicProviderProtocolError,
    AnthropicProviderSettings,
    AnthropicStructuredProvider,
    GeminiProviderCallError,
    GeminiProviderConfigurationError,
    GeminiProviderProtocolError,
    GeminiProviderSettings,
    GeminiStructuredProvider,
    KimiProviderCallError,
    KimiProviderConfigurationError,
    KimiProviderProtocolError,
    KimiProviderSettings,
    KimiStructuredProvider,
    OpenAIProviderConfigurationError,
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
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse({})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.response


class ProviderUnderTest(Protocol):
    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse: ...

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate: ...

    def capabilities(self) -> Mapping[str, Any]: ...

    def health_check(self) -> Mapping[str, Any]: ...


class CloudStructuredProviderTests(unittest.TestCase):
    def test_anthropic_uses_messages_json_schema_and_complete_usage(self) -> None:
        client = FakeClient(
            FakeResponse(
                {
                    "id": "msg_test",
                    "type": "message",
                    "model": "claude-test-snapshot",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": '{"proposals":[]}'}],
                    "usage": {
                        "input_tokens": 80,
                        "cache_read_input_tokens": 12,
                        "cache_creation_input_tokens": 8,
                        "output_tokens": 9,
                    },
                }
            )
        )
        provider = AnthropicStructuredProvider(
            client,
            AnthropicProviderSettings(
                api_key="anthropic-secret",
                model_id="claude-test",
                max_output_tokens=2_048,
            ),
            clock=self._clock(),
        )

        response = provider.generate_structured(self._request())

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual("claude-test-snapshot", response.model_id)
        self.assertEqual(100, response.input_tokens)
        self.assertEqual(12, response.cached_input_tokens)
        self.assertEqual(9, response.output_tokens)
        self.assertEqual(125, response.latency_ms)
        url, call = client.calls[0]
        self.assertEqual("https://api.anthropic.com/v1/messages", url)
        payload = call["json"]
        self.assertEqual("json_schema", payload["output_config"]["format"]["type"])
        self.assertEqual(
            self._request()["output_schema"],
            payload["output_config"]["format"]["schema"],
        )
        self.assertEqual(0, payload["temperature"])
        self.assertIn("untrusted data", payload["system"])
        self.assertEqual(
            {"purpose": "sql_generation", "input": {"question": "orders"}},
            json.loads(payload["messages"][0]["content"]),
        )

    def test_gemini_uses_generate_content_response_json_schema(self) -> None:
        client = FakeClient(
            FakeResponse(
                {
                    "modelVersion": "gemini-test-snapshot",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [{"text": '{"proposals":[]}'}],
                            },
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 75,
                        "cachedContentTokenCount": 15,
                        "candidatesTokenCount": 11,
                        "totalTokenCount": 86,
                    },
                }
            )
        )
        provider = GeminiStructuredProvider(
            client,
            GeminiProviderSettings(
                api_key="gemini-secret",
                model_id="gemini-test",
                max_output_tokens=2_048,
            ),
            clock=self._clock(),
        )

        response = provider.generate_structured(self._request())

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual("gemini-test-snapshot", response.model_id)
        self.assertEqual(75, response.input_tokens)
        self.assertEqual(15, response.cached_input_tokens)
        self.assertEqual(11, response.output_tokens)
        url, call = client.calls[0]
        self.assertEqual(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-test:generateContent",
            url,
        )
        config = call["json"]["generationConfig"]
        self.assertEqual("application/json", config["responseMimeType"])
        self.assertEqual(self._request()["output_schema"], config["responseJsonSchema"])
        self.assertEqual(0, config["temperature"])
        system_text = call["json"]["systemInstruction"]["parts"][0]["text"]
        self.assertIn("untrusted data", system_text)

    def test_kimi_uses_fixed_moonshot_endpoint_and_strict_schema(self) -> None:
        client = FakeClient(
            FakeResponse(
                {
                    "id": "cmpl_test",
                    "object": "chat.completion",
                    "model": "kimi-test-snapshot",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": '{"proposals":[]}',
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 90,
                        "completion_tokens": 13,
                        "total_tokens": 103,
                        "cached_tokens": 20,
                    },
                }
            )
        )
        provider = KimiStructuredProvider(
            client,
            KimiProviderSettings(
                api_key="moonshot-secret",
                model_id="kimi-test",
                max_output_tokens=2_048,
            ),
            clock=self._clock(),
        )

        response = provider.generate_structured(self._request())

        self.assertEqual({"proposals": []}, dict(response.payload))
        self.assertEqual("kimi-test-snapshot", response.model_id)
        self.assertEqual(90, response.input_tokens)
        self.assertEqual(20, response.cached_input_tokens)
        self.assertEqual(13, response.output_tokens)
        url, call = client.calls[0]
        self.assertEqual("https://api.moonshot.ai/v1/chat/completions", url)
        payload = call["json"]
        response_format = payload["response_format"]
        self.assertEqual("json_schema", response_format["type"])
        self.assertIs(True, response_format["json_schema"]["strict"])
        self.assertEqual(
            self._request()["output_schema"],
            response_format["json_schema"]["schema"],
        )
        self.assertIs(False, payload["stream"])
        self.assertEqual(2_048, payload["max_completion_tokens"])
        self.assertIn("untrusted data", payload["messages"][0]["content"])

    def test_estimates_capabilities_health_and_secrets_are_safe(self) -> None:
        anthropic_settings = AnthropicProviderSettings(
            api_key="secret-a",
            model_id="claude-test",
        )
        gemini_settings = GeminiProviderSettings(
            api_key="secret-g",
            model_id="gemini-test",
        )
        kimi_settings = KimiProviderSettings(
            api_key="secret-k",
            model_id="kimi-test",
        )
        cases: tuple[
            tuple[str, object, Callable[[FakeClient], ProviderUnderTest]],
            ...,
        ] = (
            (
                "anthropic",
                anthropic_settings,
                lambda client: AnthropicStructuredProvider(client, anthropic_settings),
            ),
            (
                "gemini",
                gemini_settings,
                lambda client: GeminiStructuredProvider(client, gemini_settings),
            ),
            (
                "kimi",
                kimi_settings,
                lambda client: KimiStructuredProvider(client, kimi_settings),
            ),
        )
        for provider_id, settings, provider_factory in cases:
            with self.subTest(provider_id=provider_id):
                client = FakeClient()
                provider = provider_factory(client)

                estimate = provider.count_or_estimate_tokens(self._request())

                self.assertGreater(estimate.input_tokens, 256)
                self.assertEqual(4_096, estimate.output_tokens)
                self.assertEqual(provider_id, provider.capabilities()["provider_id"])
                self.assertIs(True, provider.capabilities()["structured_output"])
                self.assertIs(False, provider.health_check()["network_checked"])
                self.assertEqual([], client.calls)
                self.assertNotIn("secret-", repr(settings))

    def test_invalid_completion_and_transport_errors_fail_closed(self) -> None:
        invalid_cases: tuple[
            tuple[Callable[[], ProviderUnderTest], type[Exception], str],
            ...,
        ] = (
            (
                lambda: AnthropicStructuredProvider(
                    FakeClient(
                        FakeResponse({"type": "message", "stop_reason": "max_tokens"})
                    ),
                    AnthropicProviderSettings(api_key="a", model_id="claude-test"),
                ),
                AnthropicProviderProtocolError,
                "not completed normally",
            ),
            (
                lambda: GeminiStructuredProvider(
                    FakeClient(FakeResponse({"candidates": []})),
                    GeminiProviderSettings(api_key="g", model_id="gemini-test"),
                ),
                GeminiProviderProtocolError,
                "exactly one candidate",
            ),
            (
                lambda: KimiStructuredProvider(
                    FakeClient(FakeResponse({"choices": []})),
                    KimiProviderSettings(api_key="k", model_id="kimi-test"),
                ),
                KimiProviderProtocolError,
                "exactly one choice",
            ),
        )
        for provider_factory, error_type, message in invalid_cases:
            with self.subTest(message=message):
                provider = provider_factory()
                with self.assertRaisesRegex(error_type, message):
                    provider.generate_structured(self._request())

        transport_error = RuntimeError("secret transport")
        transport_cases: tuple[
            tuple[Callable[[], ProviderUnderTest], type[Exception]],
            ...,
        ] = (
            (
                lambda: AnthropicStructuredProvider(
                    FakeClient(FakeResponse({}, error=transport_error)),
                    AnthropicProviderSettings(api_key="a", model_id="claude-test"),
                ),
                AnthropicProviderCallError,
            ),
            (
                lambda: GeminiStructuredProvider(
                    FakeClient(FakeResponse({}, error=transport_error)),
                    GeminiProviderSettings(api_key="g", model_id="gemini-test"),
                ),
                GeminiProviderCallError,
            ),
            (
                lambda: KimiStructuredProvider(
                    FakeClient(FakeResponse({}, error=transport_error)),
                    KimiProviderSettings(api_key="k", model_id="kimi-test"),
                ),
                KimiProviderCallError,
            ),
        )
        for provider_factory, error_type in transport_cases:
            with self.subTest(error=error_type.__name__):
                provider = provider_factory()
                with self.assertRaises(error_type) as raised:
                    provider.generate_structured(self._request())
                self.assertNotIn("secret transport", str(raised.exception))

    @staticmethod
    def _clock() -> Any:
        values = iter((100.0, 100.125))
        return lambda: next(values)

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


class MultiProviderEnvironmentTests(unittest.TestCase):
    def test_credentials_alone_never_enable_prompt_egress(self) -> None:
        providers = load_llm_providers_from_environment(
            {
                "ANTHROPIC_API_KEY": "anthropic-key",
                "SQLVERITY_ANTHROPIC_MODEL": "claude-test",
                "GEMINI_API_KEY": "gemini-key",
                "SQLVERITY_GEMINI_MODEL": "gemini-test",
                "MOONSHOT_API_KEY": "moonshot-key",
                "SQLVERITY_KIMI_MODEL": "kimi-test",
            }
        )

        self.assertEqual({}, providers)

    def test_multiple_cloud_providers_and_aliases_load_together(self) -> None:
        clients = {
            "anthropic": FakeClient(),
            "gemini": FakeClient(),
            "kimi": FakeClient(),
        }

        providers = load_llm_providers_from_environment(
            {
                "SQLVERITY_LLM_PROVIDERS": "claude, gemini, moonshot",
                "ANTHROPIC_API_KEY": "anthropic-key",
                "SQLVERITY_ANTHROPIC_MODEL": "claude-test",
                "GEMINI_API_KEY": "gemini-key",
                "SQLVERITY_GEMINI_MODEL": "gemini-test",
                "MOONSHOT_API_KEY": "moonshot-key",
                "SQLVERITY_KIMI_MODEL": "kimi-test",
            },
            anthropic_client_factory=lambda _settings: clients["anthropic"],
            gemini_client_factory=lambda _settings: clients["gemini"],
            kimi_client_factory=lambda _settings: clients["kimi"],
        )

        self.assertEqual({"anthropic", "gemini", "kimi"}, set(providers))

    def test_ambiguous_duplicate_unknown_and_incomplete_config_fail_closed(self) -> None:
        selection_cases = (
            (
                {"SQLVERITY_LLM_PROVIDER": "openai", "SQLVERITY_LLM_PROVIDERS": "gemini"},
                "either",
            ),
            ({"SQLVERITY_LLM_PROVIDERS": "claude,anthropic"}, "duplicate"),
            ({"SQLVERITY_LLM_PROVIDERS": "gemini,,kimi"}, "empty"),
            ({"SQLVERITY_LLM_PROVIDERS": "unknown"}, "Unsupported"),
        )
        for environment, message in selection_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(OpenAIProviderConfigurationError, message):
                    load_llm_providers_from_environment(environment)

        incomplete_cases = (
            (
                {"SQLVERITY_LLM_PROVIDER": "anthropic", "SQLVERITY_ANTHROPIC_MODEL": "x"},
                AnthropicProviderConfigurationError,
                "ANTHROPIC_API_KEY",
            ),
            (
                {"SQLVERITY_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "x"},
                GeminiProviderConfigurationError,
                "GEMINI_MODEL",
            ),
            (
                {"SQLVERITY_LLM_PROVIDER": "kimi", "MOONSHOT_API_KEY": "x"},
                KimiProviderConfigurationError,
                "KIMI_MODEL",
            ),
        )
        for environment, error_type, message in incomplete_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(error_type, message):
                    load_llm_providers_from_environment(environment)


if __name__ == "__main__":
    unittest.main()
