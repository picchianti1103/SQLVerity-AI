from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

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

KIMI_PROVIDER_ID = "kimi"
_CHAT_COMPLETIONS_URL = "https://api.moonshot.ai/v1/chat/completions"


class KimiProviderError(RuntimeError):
    pass


class KimiProviderConfigurationError(KimiProviderError):
    pass


class KimiProviderCallError(KimiProviderError):
    pass


class KimiProviderProtocolError(KimiProviderError):
    pass


@dataclass(frozen=True, slots=True)
class KimiProviderSettings:
    api_key: str = field(repr=False)
    model_id: str
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 4_096

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        model_id = self.model_id.strip()
        if not api_key:
            raise KimiProviderConfigurationError("MOONSHOT_API_KEY is required")
        if not model_id:
            raise KimiProviderConfigurationError("SQLVERITY_KIMI_MODEL is required")
        if not 1.0 <= self.request_timeout_seconds <= 300.0:
            raise KimiProviderConfigurationError(
                "SQLVERITY_KIMI_TIMEOUT_SECONDS must be between 1 and 300"
            )
        if not 1 <= self.max_output_tokens <= 128_000:
            raise KimiProviderConfigurationError(
                "SQLVERITY_KIMI_MAX_OUTPUT_TOKENS must be between 1 and 128000"
            )
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "model_id", model_id)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> KimiProviderSettings:
        return cls(
            api_key=environ.get("MOONSHOT_API_KEY", ""),
            model_id=environ.get("SQLVERITY_KIMI_MODEL", ""),
            request_timeout_seconds=environment_float(
                environ,
                "SQLVERITY_KIMI_TIMEOUT_SECONDS",
                default=60.0,
                error_factory=KimiProviderConfigurationError,
            ),
            max_output_tokens=environment_int(
                environ,
                "SQLVERITY_KIMI_MAX_OUTPUT_TOKENS",
                default=4_096,
                error_factory=KimiProviderConfigurationError,
            ),
        )


class KimiStructuredProvider:
    """Kimi/Moonshot Chat Completions adapter with JSON-schema output."""

    def __init__(
        self,
        client: ProviderHTTPClient,
        settings: KimiProviderSettings,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        prepared = prepare_structured_request(request, KimiProviderProtocolError)
        started_at = self._clock()
        try:
            response = self._client.post(
                _CHAT_COMPLETIONS_URL,
                json=self._request_payload(prepared),
            )
            response.raise_for_status()
            response_payload = response.json()
        except Exception as error:
            raise KimiProviderCallError("Kimi Chat Completions API request failed") from error
        latency_ms = max(0, int((self._clock() - started_at) * 1_000))
        return _parse_response(response_payload, latency_ms=latency_ms)

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        prepared = prepare_structured_request(request, KimiProviderProtocolError)
        return conservative_token_estimate(
            self._request_payload(prepared),
            max_output_tokens=self._settings.max_output_tokens,
            field="Kimi request",
            error_factory=KimiProviderProtocolError,
        )

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider_id": KIMI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "api": "chat_completions",
            "structured_output": True,
            "response_storage": "provider_policy",
        }

    def health_check(self) -> Mapping[str, Any]:
        return {
            "status": "configured",
            "provider_id": KIMI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "network_checked": False,
        }

    def close(self) -> None:
        close_if_supported(self._client)

    def _request_payload(
        self,
        prepared: PreparedStructuredRequest,
    ) -> Mapping[str, Any]:
        return {
            "model": self._settings.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{prepared.instructions.rstrip()}\n\n{SECURITY_INSTRUCTION}"
                    ),
                },
                {"role": "user", "content": prepared.input_text},
            ],
            "temperature": 0,
            "max_completion_tokens": self._settings.max_output_tokens,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": prepared.schema_name,
                    "strict": True,
                    "schema": prepared.output_schema,
                },
            },
        }


def create_kimi_client(settings: KimiProviderSettings) -> ProviderHTTPClient:
    return create_http_client(
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        timeout_seconds=settings.request_timeout_seconds,
        provider_name="Kimi",
        error_factory=KimiProviderConfigurationError,
    )


def _parse_response(response: object, *, latency_ms: int) -> LLMResponse:
    payload = required_mapping(
        response,
        "Kimi response",
        KimiProviderProtocolError,
    )
    choices = required_list(
        payload.get("choices"),
        "Kimi response choices",
        KimiProviderProtocolError,
    )
    if len(choices) != 1:
        raise KimiProviderProtocolError(
            "Kimi structured response must contain exactly one choice"
        )
    choice = required_mapping(
        choices[0],
        "Kimi response choice",
        KimiProviderProtocolError,
    )
    finish_reason = required_text(
        choice.get("finish_reason"),
        "Kimi finish_reason",
        KimiProviderProtocolError,
    )
    if finish_reason != "stop":
        raise KimiProviderProtocolError(
            f"Kimi response was not completed normally: {finish_reason}"
        )
    message = required_mapping(
        choice.get("message"),
        "Kimi response message",
        KimiProviderProtocolError,
    )
    usage = required_mapping(
        payload.get("usage"),
        "Kimi response usage",
        KimiProviderProtocolError,
    )
    prompt_tokens = nonnegative_integer(
        usage.get("prompt_tokens"),
        "Kimi usage.prompt_tokens",
        KimiProviderProtocolError,
    )
    cached_tokens = optional_nonnegative_integer(
        usage.get("cached_tokens"),
        "Kimi usage.cached_tokens",
        KimiProviderProtocolError,
    )
    if cached_tokens > prompt_tokens:
        raise KimiProviderProtocolError("Kimi cached tokens exceed prompt tokens")
    return LLMResponse(
        payload=parse_json_object(
            message.get("content"),
            field="Kimi structured output",
            error_factory=KimiProviderProtocolError,
        ),
        model_id=required_text(
            payload.get("model"),
            "Kimi response model",
            KimiProviderProtocolError,
        ),
        input_tokens=prompt_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=nonnegative_integer(
            usage.get("completion_tokens"),
            "Kimi usage.completion_tokens",
            KimiProviderProtocolError,
        ),
        latency_ms=latency_ms,
    )
