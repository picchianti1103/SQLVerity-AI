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

ANTHROPIC_PROVIDER_ID = "anthropic"
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProviderError(RuntimeError):
    pass


class AnthropicProviderConfigurationError(AnthropicProviderError):
    pass


class AnthropicProviderCallError(AnthropicProviderError):
    pass


class AnthropicProviderProtocolError(AnthropicProviderError):
    pass


@dataclass(frozen=True, slots=True)
class AnthropicProviderSettings:
    api_key: str = field(repr=False)
    model_id: str
    request_timeout_seconds: float = 60.0
    max_output_tokens: int = 4_096

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        model_id = self.model_id.strip()
        if not api_key:
            raise AnthropicProviderConfigurationError("ANTHROPIC_API_KEY is required")
        if not model_id:
            raise AnthropicProviderConfigurationError("SQLVERITY_ANTHROPIC_MODEL is required")
        if not 1.0 <= self.request_timeout_seconds <= 300.0:
            raise AnthropicProviderConfigurationError(
                "SQLVERITY_ANTHROPIC_TIMEOUT_SECONDS must be between 1 and 300"
            )
        if not 1 <= self.max_output_tokens <= 128_000:
            raise AnthropicProviderConfigurationError(
                "SQLVERITY_ANTHROPIC_MAX_OUTPUT_TOKENS must be between 1 and 128000"
            )
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "model_id", model_id)

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> AnthropicProviderSettings:
        return cls(
            api_key=environ.get("ANTHROPIC_API_KEY", ""),
            model_id=environ.get("SQLVERITY_ANTHROPIC_MODEL", ""),
            request_timeout_seconds=environment_float(
                environ,
                "SQLVERITY_ANTHROPIC_TIMEOUT_SECONDS",
                default=60.0,
                error_factory=AnthropicProviderConfigurationError,
            ),
            max_output_tokens=environment_int(
                environ,
                "SQLVERITY_ANTHROPIC_MAX_OUTPUT_TOKENS",
                default=4_096,
                error_factory=AnthropicProviderConfigurationError,
            ),
        )


class AnthropicStructuredProvider:
    """Claude Messages API adapter using GA JSON-schema structured outputs."""

    def __init__(
        self,
        client: ProviderHTTPClient,
        settings: AnthropicProviderSettings,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        prepared = prepare_structured_request(request, AnthropicProviderProtocolError)
        started_at = self._clock()
        try:
            response = self._client.post(
                _MESSAGES_URL,
                json=self._request_payload(prepared),
            )
            response.raise_for_status()
            response_payload = response.json()
        except Exception as error:
            raise AnthropicProviderCallError("Anthropic Messages API request failed") from error
        latency_ms = max(0, int((self._clock() - started_at) * 1_000))
        return _parse_response(response_payload, latency_ms=latency_ms)

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        prepared = prepare_structured_request(request, AnthropicProviderProtocolError)
        return conservative_token_estimate(
            self._request_payload(prepared),
            max_output_tokens=self._settings.max_output_tokens,
            field="Anthropic request",
            error_factory=AnthropicProviderProtocolError,
        )

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider_id": ANTHROPIC_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "api": "messages",
            "structured_output": True,
            "response_storage": "provider_policy",
        }

    def health_check(self) -> Mapping[str, Any]:
        return {
            "status": "configured",
            "provider_id": ANTHROPIC_PROVIDER_ID,
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
            "max_tokens": self._settings.max_output_tokens,
            "temperature": 0,
            "system": f"{prepared.instructions.rstrip()}\n\n{SECURITY_INSTRUCTION}",
            "messages": [{"role": "user", "content": prepared.input_text}],
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": prepared.output_schema,
                }
            },
        }


def create_anthropic_client(settings: AnthropicProviderSettings) -> ProviderHTTPClient:
    return create_http_client(
        headers={
            "x-api-key": settings.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        timeout_seconds=settings.request_timeout_seconds,
        provider_name="Anthropic",
        error_factory=AnthropicProviderConfigurationError,
    )


def _parse_response(response: object, *, latency_ms: int) -> LLMResponse:
    payload = required_mapping(
        response,
        "Anthropic response",
        AnthropicProviderProtocolError,
    )
    if payload.get("type") != "message":
        raise AnthropicProviderProtocolError("Anthropic response is not a message")
    stop_reason = required_text(
        payload.get("stop_reason"),
        "Anthropic stop_reason",
        AnthropicProviderProtocolError,
    )
    if stop_reason != "end_turn":
        raise AnthropicProviderProtocolError(
            f"Anthropic response was not completed normally: {stop_reason}"
        )
    content = required_list(
        payload.get("content"),
        "Anthropic response content",
        AnthropicProviderProtocolError,
    )
    text_blocks = [
        block.get("text")
        for item in content
        if isinstance(item, Mapping)
        and (block := item).get("type") == "text"
    ]
    if len(text_blocks) != 1:
        raise AnthropicProviderProtocolError(
            "Anthropic structured response must contain exactly one text block"
        )
    usage = required_mapping(
        payload.get("usage"),
        "Anthropic response usage",
        AnthropicProviderProtocolError,
    )
    regular_input = nonnegative_integer(
        usage.get("input_tokens"),
        "Anthropic usage.input_tokens",
        AnthropicProviderProtocolError,
    )
    cached_input = optional_nonnegative_integer(
        usage.get("cache_read_input_tokens"),
        "Anthropic usage.cache_read_input_tokens",
        AnthropicProviderProtocolError,
    )
    cache_creation = optional_nonnegative_integer(
        usage.get("cache_creation_input_tokens"),
        "Anthropic usage.cache_creation_input_tokens",
        AnthropicProviderProtocolError,
    )
    return LLMResponse(
        payload=parse_json_object(
            text_blocks[0],
            field="Anthropic structured output",
            error_factory=AnthropicProviderProtocolError,
        ),
        model_id=required_text(
            payload.get("model"),
            "Anthropic response model",
            AnthropicProviderProtocolError,
        ),
        input_tokens=regular_input + cached_input + cache_creation,
        cached_input_tokens=cached_input,
        output_tokens=nonnegative_integer(
            usage.get("output_tokens"),
            "Anthropic usage.output_tokens",
            AnthropicProviderProtocolError,
        ),
        latency_ms=latency_ms,
    )
