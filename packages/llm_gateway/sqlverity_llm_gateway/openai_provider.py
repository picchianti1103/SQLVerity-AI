from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from time import perf_counter
from typing import Any, Protocol, cast

from packages.domain.sqlverity_domain.contracts import LLMProvider, LLMResponse, TokenEstimate

from .anthropic_provider import (
    ANTHROPIC_PROVIDER_ID,
    AnthropicProviderSettings,
    AnthropicStructuredProvider,
    create_anthropic_client,
)
from .gemini_provider import (
    GEMINI_PROVIDER_ID,
    GeminiProviderSettings,
    GeminiStructuredProvider,
    create_gemini_client,
)
from .kimi_provider import (
    KIMI_PROVIDER_ID,
    KimiProviderSettings,
    KimiStructuredProvider,
    create_kimi_client,
)
from .ollama_provider import (
    OLLAMA_PROVIDER_ID,
    OllamaHTTPClient,
    OllamaProviderSettings,
    OllamaStructuredProvider,
    create_ollama_client,
)
from .provider_http import (
    SECURITY_INSTRUCTION,
    ProviderHTTPClient,
    close_if_supported,
    conservative_token_estimate,
    environment_float,
    environment_int,
    nonnegative_integer,
    parse_json_object,
    prepare_structured_request,
    required_text,
)

OPENAI_PROVIDER_ID = "openai"
_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
class OpenAIProviderError(RuntimeError):
    pass


class OpenAIProviderConfigurationError(OpenAIProviderError):
    pass


class OpenAIProviderCallError(OpenAIProviderError):
    pass


class OpenAIProviderProtocolError(OpenAIProviderError):
    pass


class _ResponsesResource(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _OpenAIClient(Protocol):
    @property
    def responses(self) -> _ResponsesResource: ...


@dataclass(frozen=True, slots=True)
class OpenAIProviderSettings:
    api_key: str = field(repr=False)
    model_id: str
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    max_output_tokens: int = 4_096

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise OpenAIProviderConfigurationError("OPENAI_API_KEY is required")
        if not self.model_id.strip():
            raise OpenAIProviderConfigurationError("SQLVERITY_OPENAI_MODEL is required")
        if not 1.0 <= self.request_timeout_seconds <= 300.0:
            raise OpenAIProviderConfigurationError(
                "SQLVERITY_OPENAI_TIMEOUT_SECONDS must be between 1 and 300"
            )
        if not 0 <= self.max_retries <= 10:
            raise OpenAIProviderConfigurationError(
                "SQLVERITY_OPENAI_MAX_RETRIES must be between 0 and 10"
            )
        if not 1 <= self.max_output_tokens <= 128_000:
            raise OpenAIProviderConfigurationError(
                "SQLVERITY_OPENAI_MAX_OUTPUT_TOKENS must be between 1 and 128000"
            )
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "model_id", self.model_id.strip())

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
    ) -> OpenAIProviderSettings:
        return cls(
            api_key=environ.get("OPENAI_API_KEY", ""),
            model_id=environ.get("SQLVERITY_OPENAI_MODEL", ""),
            request_timeout_seconds=environment_float(
                environ,
                "SQLVERITY_OPENAI_TIMEOUT_SECONDS",
                default=60.0,
                error_factory=OpenAIProviderConfigurationError,
            ),
            max_retries=environment_int(
                environ,
                "SQLVERITY_OPENAI_MAX_RETRIES",
                default=2,
                error_factory=OpenAIProviderConfigurationError,
            ),
            max_output_tokens=environment_int(
                environ,
                "SQLVERITY_OPENAI_MAX_OUTPUT_TOKENS",
                default=4_096,
                error_factory=OpenAIProviderConfigurationError,
            ),
        )


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter for SQLVerity AI's provider-neutral LLM contract."""

    def __init__(
        self,
        client: _OpenAIClient,
        settings: OpenAIProviderSettings,
        *,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock

    def generate_structured(self, request: Mapping[str, Any]) -> LLMResponse:
        prepared = prepare_structured_request(request, OpenAIProviderProtocolError)
        started_at = self._clock()
        try:
            response = self._client.responses.create(
                model=self._settings.model_id,
                instructions=f"{prepared.instructions.rstrip()}\n\n{SECURITY_INSTRUCTION}",
                input=prepared.input_text,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": prepared.schema_name,
                        "strict": True,
                        "schema": prepared.output_schema,
                    }
                },
                max_output_tokens=self._settings.max_output_tokens,
                store=False,
            )
        except Exception as error:
            raise OpenAIProviderCallError("OpenAI Responses API request failed") from error
        latency_ms = max(0, int((self._clock() - started_at) * 1_000))
        return _parse_response(response, latency_ms=latency_ms)

    def count_or_estimate_tokens(self, request: Mapping[str, Any]) -> TokenEstimate:
        prepared = prepare_structured_request(request, OpenAIProviderProtocolError)
        return conservative_token_estimate(
            {
                "instructions": prepared.instructions,
                "input": prepared.input_text,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": prepared.schema_name,
                        "strict": True,
                        "schema": prepared.output_schema,
                    }
                },
            },
            max_output_tokens=self._settings.max_output_tokens,
            field="OpenAI request",
            error_factory=OpenAIProviderProtocolError,
        )

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider_id": OPENAI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "api": "responses",
            "structured_output": True,
            "response_storage": False,
        }

    def health_check(self) -> Mapping[str, Any]:
        return {
            "status": "configured",
            "provider_id": OPENAI_PROVIDER_ID,
            "model_id": self._settings.model_id,
            "network_checked": False,
        }

    def close(self) -> None:
        close_if_supported(self._client)


def load_llm_providers_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[OpenAIProviderSettings], _OpenAIClient] | None = None,
    ollama_client_factory: Callable[[OllamaProviderSettings], OllamaHTTPClient] | None = None,
    anthropic_client_factory: (
        Callable[[AnthropicProviderSettings], ProviderHTTPClient] | None
    ) = None,
    gemini_client_factory: Callable[[GeminiProviderSettings], ProviderHTTPClient] | None = None,
    kimi_client_factory: Callable[[KimiProviderSettings], ProviderHTTPClient] | None = None,
) -> dict[str, LLMProvider]:
    environment = os.environ if environ is None else environ
    selected_providers = _selected_provider_ids(environment)
    providers: dict[str, LLMProvider] = {}
    for selected_provider in selected_providers:
        if selected_provider == OLLAMA_PROVIDER_ID:
            ollama_settings = OllamaProviderSettings.from_environment(environment)
            ollama_factory = (
                create_ollama_client
                if ollama_client_factory is None
                else ollama_client_factory
            )
            providers[OLLAMA_PROVIDER_ID] = OllamaStructuredProvider(
                ollama_factory(ollama_settings),
                ollama_settings,
            )
        elif selected_provider == OPENAI_PROVIDER_ID:
            settings = OpenAIProviderSettings.from_environment(environment)
            factory = _create_official_client if client_factory is None else client_factory
            providers[OPENAI_PROVIDER_ID] = OpenAIResponsesProvider(
                factory(settings),
                settings,
            )
        elif selected_provider == ANTHROPIC_PROVIDER_ID:
            anthropic_settings = AnthropicProviderSettings.from_environment(environment)
            anthropic_factory = (
                create_anthropic_client
                if anthropic_client_factory is None
                else anthropic_client_factory
            )
            providers[ANTHROPIC_PROVIDER_ID] = AnthropicStructuredProvider(
                anthropic_factory(anthropic_settings),
                anthropic_settings,
            )
        elif selected_provider == GEMINI_PROVIDER_ID:
            gemini_settings = GeminiProviderSettings.from_environment(environment)
            gemini_factory = (
                create_gemini_client
                if gemini_client_factory is None
                else gemini_client_factory
            )
            providers[GEMINI_PROVIDER_ID] = GeminiStructuredProvider(
                gemini_factory(gemini_settings),
                gemini_settings,
            )
        elif selected_provider == KIMI_PROVIDER_ID:
            kimi_settings = KimiProviderSettings.from_environment(environment)
            kimi_factory = (
                create_kimi_client
                if kimi_client_factory is None
                else kimi_client_factory
            )
            providers[KIMI_PROVIDER_ID] = KimiStructuredProvider(
                kimi_factory(kimi_settings),
                kimi_settings,
            )
    return providers


def _selected_provider_ids(environ: Mapping[str, str]) -> tuple[str, ...]:
    legacy = environ.get("SQLVERITY_LLM_PROVIDER", "").strip()
    multiple = environ.get("SQLVERITY_LLM_PROVIDERS", "").strip()
    if legacy and multiple:
        raise OpenAIProviderConfigurationError(
            "Configure either SQLVERITY_LLM_PROVIDER or SQLVERITY_LLM_PROVIDERS, not both"
        )
    raw_selection = multiple or legacy
    if not raw_selection:
        return ()
    raw_items = raw_selection.split(",")
    if any(not item.strip() for item in raw_items):
        raise OpenAIProviderConfigurationError(
            "SQLVERITY_LLM_PROVIDERS contains an empty provider id"
        )
    aliases = {
        "claude": ANTHROPIC_PROVIDER_ID,
        "moonshot": KIMI_PROVIDER_ID,
    }
    selected = tuple(
        aliases.get(item.strip().casefold(), item.strip().casefold())
        for item in raw_items
    )
    if len(selected) != len(set(selected)):
        raise OpenAIProviderConfigurationError(
            "SQLVERITY_LLM_PROVIDERS contains duplicate provider ids"
        )
    supported = {
        OPENAI_PROVIDER_ID,
        OLLAMA_PROVIDER_ID,
        ANTHROPIC_PROVIDER_ID,
        GEMINI_PROVIDER_ID,
        KIMI_PROVIDER_ID,
    }
    unsupported = tuple(item for item in selected if item not in supported)
    if unsupported:
        raise OpenAIProviderConfigurationError(
            f"Unsupported SQLVERITY_LLM_PROVIDER: {', '.join(unsupported)}"
        )
    return selected


def _create_official_client(settings: OpenAIProviderSettings) -> _OpenAIClient:
    try:
        openai_module = import_module("openai")
        client_constructor = cast(Callable[..., object], openai_module.OpenAI)
    except (AttributeError, ImportError) as error:
        raise OpenAIProviderConfigurationError(
            "The official OpenAI Python SDK is required when SQLVERITY_LLM_PROVIDER=openai"
        ) from error
    client = client_constructor(
        api_key=settings.api_key,
        base_url=_OPENAI_API_BASE_URL,
        timeout=settings.request_timeout_seconds,
        max_retries=settings.max_retries,
    )
    return cast(_OpenAIClient, client)


def _parse_response(response: object, *, latency_ms: int) -> LLMResponse:
    status = getattr(response, "status", None)
    if status != "completed":
        raise OpenAIProviderProtocolError("OpenAI response was not completed")
    model_id = required_text(
        getattr(response, "model", None),
        "response model",
        OpenAIProviderProtocolError,
    )
    payload = parse_json_object(
        getattr(response, "output_text", None),
        field="OpenAI structured output",
        error_factory=OpenAIProviderProtocolError,
    )

    usage = getattr(response, "usage", None)
    if usage is None:
        raise OpenAIProviderProtocolError("OpenAI response has no usage telemetry")
    input_tokens = nonnegative_integer(
        getattr(usage, "input_tokens", None),
        "usage.input_tokens",
        OpenAIProviderProtocolError,
    )
    output_tokens = nonnegative_integer(
        getattr(usage, "output_tokens", None),
        "usage.output_tokens",
        OpenAIProviderProtocolError,
    )
    input_details = getattr(usage, "input_tokens_details", None)
    cached_input_tokens = (
        0
        if input_details is None
        else nonnegative_integer(
            getattr(input_details, "cached_tokens", 0),
            "usage.input_tokens_details.cached_tokens",
            OpenAIProviderProtocolError,
        )
    )
    if cached_input_tokens > input_tokens:
        raise OpenAIProviderProtocolError("Cached input tokens exceed input tokens")
    return LLMResponse(
        payload=payload,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        latency_ms=latency_ms,
    )
