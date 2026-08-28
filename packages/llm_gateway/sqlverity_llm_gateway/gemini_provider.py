from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from urllib.parse import quote

from packages.domain.sqlverity_domain.contracts import LLMResponse, TokenEstimate

from .provider_http import (
    SECURITY_INSTRUCTION,
    PreparedStructuredRequest,
    ProviderHTTPClient,
    close_if_supported,
    conservative_token_estimate,
    create_http_client,
    environment_float,
    environment_int,
    nonnegative_integer,
    optional_nonnegative_integer,
    parse_json_object,
    prepare_structured_request,
    required_list,
    required_mapping,
    required_text,
)

GEMINI_PROVIDER_ID = "gemini"
_GENERATE_CONTENT_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProviderError(RuntimeError):
    pass


class GeminiProviderConfigurationError(GeminiProviderError):
    pass


class GeminiProviderCallError(GeminiProviderError):
    pass


class GeminiProviderProtocolError(GeminiProviderError):
    pass


@dataclass(frozen=True, slots=True)
class GeminiProviderSettings:
    api_key: str = field(repr=False)
    model_id: str
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 4_096

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        model_id = self.model_id.strip()
        if not api_key:
            raise GeminiProviderConfigurationError("GEMINI_API_KEY is required")
        if not model_id:
            raise GeminiProviderConfigurationError("SQLVERITY_GEMINI_MODEL is required")
        if not 1.0 <= self.request_timeout_seconds <= 300.0:
            raise GeminiProviderConfigurationError(
                "SQLVERITY_GEMINI_TIMEOUT_SECONDS must be between 1 and 300"
            )
        if not 1 <= self.max_output_tokens <= 128_000:
            raise GeminiProviderConfigurationError(
                "SQLVERITY_GEMINI_MAX_OUTPUT_TOKENS must be between 1 and 128000"
            )
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "model_id", model_id)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> GeminiProviderSettings:
        return cls(
            api_key=environ.get("GEMINI_API_KEY", ""),
            model_id=environ.get("SQLVERITY_GEMINI_MODEL", ""),
            request_timeout_seconds=environment_float(
                environ,
                "SQLVERITY_GEMINI_TIMEOUT_SECONDS",
                default=60.0,
                error_factory=GeminiProviderConfigurationError,
            ),
            max_output_tokens=environment_int(
                environ,
                "SQLVERITY_GEMINI_MAX_OUTPUT_TOKENS",
                default=4_096,
                error_factory=GeminiProviderConfigurationError,
            ),
        )


class GeminiStructuredProvider:
    """Gemini GenerateContent adapter with JSON-schema constrained output."""

    def __init__(
        self,
        client: ProviderHTTPClient,
        settings: GeminiProviderSettings,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        prepared = prepare_structured_request(request, GeminiProviderProtocolError)
        started_at = self._clock()
        try:
            response = self._client.post(
                self._endpoint(),
                json=self._request_payload(prepared),
            )
            response.raise_for_status()
            response_payload = response.json()
        except Exception as error:
            raise GeminiProviderCallError("Gemini GenerateContent API request failed") from error
        latency_ms = max(0, int((self._clock() - started_at) * 1_000))
        return _parse_response(response_payload, latency_ms=latency_ms)

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        prepared = prepare_structured_request(request, GeminiProviderProtocolError)
        return conservative_token_estimate(
            self._request_payload(prepared),
            max_output_tokens=self._settings.max_output_tokens,
            field="Gemini request",
            error_factory=GeminiProviderProtocolError,
        )

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider_id": GEMINI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "api": "generateContent",
            "structured_output": True,
            "response_storage": "provider_policy",
        }

    def health_check(self) -> Mapping[str, Any]:
        return {
            "status": "configured",
            "provider_id": GEMINI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "network_checked": False,
        }

    def close(self) -> None:
        close_if_supported(self._client)

    def _endpoint(self) -> str:
        encoded_model = quote(self._settings.model_id, safe="")
        return f"{_GENERATE_CONTENT_ROOT}/{encoded_model}:generateContent"

    def _request_payload(
        self,
        prepared: PreparedStructuredRequest,
    ) -> Mapping[str, Any]:
        return {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            f"{prepared.instructions.rstrip()}\n\n{SECURITY_INSTRUCTION}"
                        )
                    }
                ]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prepared.input_text}],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self._settings.max_output_tokens,
                "responseMimeType": "application/json",
                "responseJsonSchema": prepared.output_schema,
            },
        }


def create_gemini_client(settings: GeminiProviderSettings) -> ProviderHTTPClient:
    return create_http_client(
        headers={
            "x-goog-api-key": settings.api_key,
            "content-type": "application/json",
        },
        timeout_seconds=settings.request_timeout_seconds,
        provider_name="Gemini",
        error_factory=GeminiProviderConfigurationError,
    )


def _parse_response(response: object, *, latency_ms: int) -> LLMResponse:
    payload = required_mapping(
        response,
        "Gemini response",
        GeminiProviderProtocolError,
    )
    candidates = required_list(
        payload.get("candidates"),
        "Gemini response candidates",
        GeminiProviderProtocolError,
    )
    if len(candidates) != 1:
        raise GeminiProviderProtocolError(
            "Gemini structured response must contain exactly one candidate"
        )
    candidate = required_mapping(
        candidates[0],
        "Gemini response candidate",
        GeminiProviderProtocolError,
    )
    finish_reason = required_text(
        candidate.get("finishReason"),
        "Gemini finishReason",
        GeminiProviderProtocolError,
    )
    if finish_reason != "STOP":
        raise GeminiProviderProtocolError(
            f"Gemini response was not completed normally: {finish_reason}"
        )
    content = required_mapping(
        candidate.get("content"),
        "Gemini response content",
        GeminiProviderProtocolError,
    )
    parts = required_list(
        content.get("parts"),
        "Gemini response parts",
        GeminiProviderProtocolError,
    )
    text_parts = [item.get("text") for item in parts if isinstance(item, Mapping)]
    if len(text_parts) != 1:
        raise GeminiProviderProtocolError(
            "Gemini structured response must contain exactly one text part"
        )
    usage = required_mapping(
        payload.get("usageMetadata"),
        "Gemini usageMetadata",
        GeminiProviderProtocolError,
    )
    prompt_tokens = nonnegative_integer(
        usage.get("promptTokenCount"),
        "Gemini usage.promptTokenCount",
        GeminiProviderProtocolError,
    )
    cached_tokens = optional_nonnegative_integer(
        usage.get("cachedContentTokenCount"),
        "Gemini usage.cachedContentTokenCount",
        GeminiProviderProtocolError,
    )
    if cached_tokens > prompt_tokens:
        raise GeminiProviderProtocolError("Gemini cached tokens exceed prompt tokens")
    return LLMResponse(
        payload=parse_json_object(
            text_parts[0],
            field="Gemini structured output",
            error_factory=GeminiProviderProtocolError,
        ),
        model_id=required_text(
            payload.get("modelVersion"),
            "Gemini response modelVersion",
            GeminiProviderProtocolError,
        ),
        input_tokens=prompt_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=nonnegative_integer(
            usage.get("candidatesTokenCount"),
            "Gemini usage.candidatesTokenCount",
            GeminiProviderProtocolError,
        ),
        latency_ms=latency_ms,
    )
