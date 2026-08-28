from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from packages.cost_engine.sqlverity_cost_engine import CostEstimate, FinOpsService
from packages.domain.sqlverity_domain.contracts import (
    LLMProvider,
    LLMResponse,
    LLMUsageRecorder,
    PolicyDecision,
    PolicyEngine,
    TokenEstimate,
)
from packages.domain.sqlverity_domain.models import Classification, LLMUsageEvent

from .provider_http import close_if_supported


class LLMGatewayError(RuntimeError):
    pass


class LLMProviderNotFoundError(LLMGatewayError):
    pass


class LLMProviderCapabilityError(LLMGatewayError):
    pass


class LLMProviderCallError(LLMGatewayError):
    pass


class PromptEgressBlockedError(LLMGatewayError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "prompt_egress_blocked",
        provider_id: str | None = None,
        purpose: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        redacted_required_items: Sequence[Mapping[str, str]] = (),
        next_actions: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.purpose = purpose
        self.metadata = MappingProxyType(dict(metadata or {}))
        self.redacted_required_items = tuple(
            MappingProxyType(dict(item)) for item in redacted_required_items
        )
        self.next_actions = tuple(next_actions)

    def safe_detail(self) -> Mapping[str, Any]:
        detail: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "provider_invoked": False,
        }
        if self.provider_id is not None:
            detail["provider_id"] = self.provider_id
        if self.purpose is not None:
            detail["purpose"] = self.purpose
        detail.update(self.metadata)
        if self.redacted_required_items:
            detail["redacted_required_items"] = tuple(
                dict(item) for item in self.redacted_required_items
            )
        if self.next_actions:
            detail["next_actions"] = self.next_actions
        return MappingProxyType(detail)


class LLMBudgetExceededError(LLMGatewayError):
    pass


@dataclass(frozen=True, slots=True)
class PromptContentItem:
    id: str
    kind: str
    classification: Classification
    content: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.kind.strip():
            raise ValueError("Prompt content requires id and kind")
        object.__setattr__(self, "content", MappingProxyType(dict(self.content)))


@dataclass(frozen=True, slots=True)
class StructuredLLMRequest:
    purpose: str
    instructions: str
    content: tuple[PromptContentItem, ...]
    output_schema: Mapping[str, Any]
    required_content_ids: frozenset[str] = frozenset()
    privacy_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.purpose.strip() or not self.instructions.strip():
            raise ValueError("Structured LLM request requires purpose and instructions")
        if not self.content:
            raise ValueError("Structured LLM request requires content")
        content_ids = tuple(item.id for item in self.content)
        if len(content_ids) != len(set(content_ids)):
            raise ValueError("Structured LLM request content ids must be unique")
        if not self.required_content_ids.issubset(content_ids):
            raise ValueError("Required prompt content ids must exist in the request")
        object.__setattr__(self, "output_schema", MappingProxyType(dict(self.output_schema)))
        object.__setattr__(
            self,
            "privacy_context",
            MappingProxyType(dict(self.privacy_context)),
        )


@dataclass(frozen=True, slots=True)
class LLMPreflightResult:
    provider_id: str
    model_id: str
    purpose: str
    policy_decision: PolicyDecision
    content_manifest: tuple[Mapping[str, Any], ...]
    included_content_ids: frozenset[str]
    redacted_content_ids: frozenset[str]
    manifest_digest: str
    provider_invoked: bool = False


@dataclass(frozen=True, slots=True)
class LLMGatewayResult:
    response: LLMResponse
    estimate: TokenEstimate
    policy_decision: PolicyDecision
    included_content_ids: frozenset[str]
    usage: LLMUsageEvent


class LLMGateway:
    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        policy_engine: PolicyEngine,
        usage_recorder: LLMUsageRecorder,
        finops: FinOpsService | None = None,
    ) -> None:
        self._providers = {
            provider_id.strip(): provider
            for provider_id, provider in providers.items()
            if provider_id.strip()
        }
        self._policy_engine = policy_engine
        self._usage_recorder = usage_recorder
        self._finops = finops

    def close(self) -> None:
        for provider in self._providers.values():
            close_if_supported(provider)

    def generate_structured(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        request: StructuredLLMRequest,
        data_source_id: str | None = None,
    ) -> LLMGatewayResult:
        preflight = self.preflight_structured(
            tenant_id=tenant_id,
            provider_id=provider_id,
            request=request,
            data_source_id=data_source_id,
        )
        provider = self._providers[provider_id]
        decision = preflight.policy_decision
        if not decision.allowed:
            reasons = "; ".join(decision.reasons) or "Prompt egress denied by policy"
            raise self._blocked_error(
                reasons,
                provider_id=provider_id,
                request=request,
                decision=decision,
            )

        redacted_ids = preflight.redacted_content_ids
        known_ids = frozenset(item.id for item in request.content)
        if not redacted_ids.issubset(known_ids):
            raise self._blocked_error(
                "Policy returned unknown redaction identifiers",
                provider_id=provider_id,
                request=request,
                decision=decision,
                code="invalid_policy_redaction",
            )
        redacted_required_ids = request.required_content_ids & redacted_ids
        if redacted_required_ids:
            kinds = {item.id: item.kind for item in request.content}
            raise self._blocked_error(
                "Policy redacted required prompt content",
                provider_id=provider_id,
                request=request,
                decision=decision,
                code="required_prompt_content_redacted",
                redacted_required_items=tuple(
                    {"id": content_id, "kind": kinds[content_id]}
                    for content_id in sorted(redacted_required_ids)
                ),
                next_actions=("remove_sensitive_literal", "review_provider_policy"),
            )
        included = tuple(item for item in request.content if item.id not in redacted_ids)
        if not included:
            raise self._blocked_error(
                "Policy redacted all prompt content",
                provider_id=provider_id,
                request=request,
                decision=decision,
                code="all_prompt_content_redacted",
                next_actions=("review_provider_policy",),
            )
        payload = _provider_payload(request, included)

        precall_cost: CostEstimate | None = None
        try:
            capabilities = provider.capabilities()
            if capabilities.get("structured_output") is not True:
                raise LLMProviderCapabilityError(
                    f"LLM provider {provider_id} does not guarantee structured output"
                )
            estimate = provider.count_or_estimate_tokens(payload)
            declared_model = capabilities.get("model_id")
            if self._finops is not None:
                if not isinstance(declared_model, str) or not declared_model.strip():
                    if self._finops.has_active_budget(tenant_id):
                        raise LLMBudgetExceededError(
                            "Active budget requires the provider to declare its model id"
                        )
                else:
                    precall_cost = self._finops.estimate(
                        tenant_id=tenant_id,
                        provider_id=provider_id,
                        model_id=declared_model,
                        input_tokens=estimate.input_tokens,
                        cached_input_tokens=estimate.cached_input_tokens,
                        output_tokens=estimate.output_tokens,
                    )
                    if precall_cost is None:
                        if self._finops.has_active_budget(tenant_id):
                            raise LLMBudgetExceededError(
                                "Active budget requires applicable model pricing"
                            )
                    else:
                        budget = self._finops.authorize(tenant_id, precall_cost)
                        if not budget.allowed:
                            raise LLMBudgetExceededError(budget.reason)
            response = provider.generate_structured(payload)
        except LLMGatewayError:
            raise
        except Exception as error:
            raise LLMProviderCallError(f"LLM provider {provider_id} failed") from error

        priced_estimate = (
            self._finops.estimate(
                tenant_id=tenant_id,
                provider_id=provider_id,
                model_id=response.model_id,
                input_tokens=estimate.input_tokens,
                cached_input_tokens=estimate.cached_input_tokens,
                output_tokens=estimate.output_tokens,
            )
            if self._finops is not None
            else None
        )
        actual_cost = (
            self._finops.estimate(
                tenant_id=tenant_id,
                provider_id=provider_id,
                model_id=response.model_id,
                input_tokens=response.input_tokens,
                cached_input_tokens=response.cached_input_tokens,
                output_tokens=response.output_tokens,
            )
            if self._finops is not None
            else None
        )
        applied_pricing = actual_cost or priced_estimate or precall_cost
        usage = LLMUsageEvent(
            tenant_id=tenant_id,
            provider_id=provider_id,
            model_id=response.model_id,
            purpose=request.purpose,
            estimated_input_tokens=estimate.input_tokens,
            estimated_output_tokens=estimate.output_tokens,
            input_tokens=response.input_tokens,
            cached_input_tokens=response.cached_input_tokens,
            output_tokens=response.output_tokens,
            latency_ms=response.latency_ms,
            estimated_cost=(
                priced_estimate.amount_text
                if priced_estimate is not None
                else estimate.estimated_cost
            ),
            actual_cost=actual_cost.amount_text if actual_cost is not None else None,
            currency=(
                applied_pricing.currency if applied_pricing is not None else None
            ),
            pricing_id=(
                applied_pricing.pricing_id if applied_pricing is not None else None
            ),
        )
        self._usage_recorder.record_llm_usage(usage)
        return LLMGatewayResult(
            response=response,
            estimate=estimate,
            policy_decision=decision,
            included_content_ids=frozenset(item.id for item in included),
            usage=usage,
        )

    def preflight_structured(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        request: StructuredLLMRequest,
        data_source_id: str | None = None,
    ) -> LLMPreflightResult:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise LLMProviderNotFoundError(f"LLM provider {provider_id} is not configured")
        capabilities = provider.capabilities()
        if capabilities.get("structured_output") is not True:
            raise LLMProviderCapabilityError(
                f"LLM provider {provider_id} does not guarantee structured output"
            )
        model_value = capabilities.get("model_id")
        model_id = (
            model_value.strip()
            if isinstance(model_value, str) and model_value.strip()
            else "unreported"
        )
        manifest = tuple(_content_manifest(item) for item in request.content)
        decision = self._policy_engine.evaluate_prompt_egress(
            tenant_id=tenant_id,
            provider_id=provider_id,
            content_manifest=manifest,
            data_source_id=data_source_id,
            purpose=request.purpose,
        )
        redacted_ids = frozenset(decision.redacted_fields)
        included_ids = frozenset(
            item.id for item in request.content if item.id not in redacted_ids
        )
        return LLMPreflightResult(
            provider_id=provider_id,
            model_id=model_id,
            purpose=request.purpose,
            policy_decision=decision,
            content_manifest=manifest,
            included_content_ids=included_ids,
            redacted_content_ids=redacted_ids,
            manifest_digest=_request_manifest_digest(request),
        )

    @staticmethod
    def _blocked_error(
        message: str,
        *,
        provider_id: str,
        request: StructuredLLMRequest,
        decision: PolicyDecision,
        code: str | None = None,
        redacted_required_items: Sequence[Mapping[str, str]] = (),
        next_actions: Sequence[str] = (),
    ) -> PromptEgressBlockedError:
        metadata = dict(request.privacy_context)
        metadata.update(dict(decision.metadata))
        metadata["maximum_allowed_classification"] = (
            decision.maximum_classification.value
        )
        decision_code = code or str(
            decision.metadata.get("decision_code", "prompt_egress_blocked")
        )
        if not next_actions:
            if decision_code == "missing_policy":
                next_actions = ("configure_provider_policy",)
            elif decision_code in {
                "denied_provider",
                "denied_purpose",
                "residency_mismatch",
                "retention_mismatch",
            }:
                next_actions = ("review_provider_policy",)
        return PromptEgressBlockedError(
            message,
            code=decision_code,
            provider_id=provider_id,
            purpose=request.purpose,
            metadata=metadata,
            redacted_required_items=redacted_required_items,
            next_actions=next_actions,
        )


def _content_manifest(item: PromptContentItem) -> Mapping[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "classification": item.classification.value,
        "fields": tuple(sorted(item.content)),
    }


def _request_manifest_digest(request: StructuredLLMRequest) -> str:
    canonical = {
        "purpose": request.purpose,
        "instructions": request.instructions,
        "content": tuple(
            {
                "id": item.id,
                "kind": item.kind,
                "classification": item.classification.value,
                "content": dict(item.content),
            }
            for item in request.content
        ),
        "output_schema": dict(request.output_schema),
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_payload(
    request: StructuredLLMRequest,
    content: Sequence[PromptContentItem],
) -> Mapping[str, Any]:
    return {
        "purpose": request.purpose,
        "instructions": request.instructions,
        "input": {
            "trust_level": "untrusted_data",
            "items": tuple(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "data": dict(item.content),
                }
                for item in content
            ),
        },
        "output_schema": dict(request.output_schema),
    }
