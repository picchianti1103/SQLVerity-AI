from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from packages.domain.sqlverity_domain.contracts import PolicyDecision
from packages.domain.sqlverity_domain.models import Classification, ProviderEgressPolicy

_CLASSIFICATION_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.PII: 3,
    Classification.HIGHLY_SENSITIVE: 4,
}

_SCHEMA_METADATA_KINDS = frozenset(
    {"schema_object", "schema_column", "schema_relationship"}
)


class ProviderPolicyRepository(Protocol):
    def get_effective_provider_egress_policy(
        self,
        tenant_id: str,
        provider_id: str,
        data_source_id: str | None,
    ) -> ProviderEgressPolicy | None: ...


class MetadataOnlyPolicyEngine:
    """Minimal fail-closed policy for schema-only LLM prompts."""

    def __init__(
        self,
        *,
        allowed_provider_ids: frozenset[str] = frozenset(),
        maximum_classification: Classification = Classification.INTERNAL,
        allowed_content_kinds: frozenset[str] = _SCHEMA_METADATA_KINDS,
    ) -> None:
        self._allowed_provider_ids = allowed_provider_ids
        self._maximum_classification = maximum_classification
        self._allowed_content_kinds = allowed_content_kinds

    def evaluate_prompt_egress(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        content_manifest: Sequence[Mapping[str, Any]],
        data_source_id: str | None = None,
        purpose: str | None = None,
    ) -> PolicyDecision:
        return self._evaluate_prompt_manifest(
            tenant_id=tenant_id,
            provider_id=provider_id,
            content_manifest=content_manifest,
            maximum_classification=self._maximum_classification,
        )

    def _evaluate_prompt_manifest(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        content_manifest: Sequence[Mapping[str, Any]],
        maximum_classification: Classification,
        metadata: Mapping[str, Any] | None = None,
    ) -> PolicyDecision:
        if not tenant_id.strip():
            return PolicyDecision(
                allowed=False,
                reasons=("Tenant boundary is required",),
                metadata={"decision_code": "invalid_tenant_boundary"},
            )
        if self._allowed_provider_ids and provider_id not in self._allowed_provider_ids:
            return PolicyDecision(
                allowed=False,
                reasons=(f"Provider {provider_id} is not allowed for prompt egress",),
                maximum_classification=maximum_classification,
                metadata={"decision_code": "denied_provider"},
            )

        redacted_ids: list[str] = []
        for entry in content_manifest:
            content_id = entry.get("id")
            kind = entry.get("kind")
            classification_value = entry.get("classification")
            if not isinstance(content_id, str) or not content_id.strip():
                return PolicyDecision(
                    allowed=False,
                    reasons=("Manifest item has no id",),
                    metadata={"decision_code": "invalid_manifest"},
                )
            if kind not in self._allowed_content_kinds:
                return PolicyDecision(
                    allowed=False,
                    reasons=(f"Content kind {kind!s} is not schema metadata",),
                    maximum_classification=maximum_classification,
                    metadata={"decision_code": "invalid_content_kind"},
                )
            if not isinstance(classification_value, str):
                return PolicyDecision(
                    allowed=False,
                    reasons=(f"Manifest item {content_id} has invalid classification",),
                    maximum_classification=maximum_classification,
                    metadata={"decision_code": "invalid_manifest"},
                )
            try:
                classification = Classification(classification_value)
            except (TypeError, ValueError):
                return PolicyDecision(
                    allowed=False,
                    reasons=(f"Manifest item {content_id} has invalid classification",),
                    maximum_classification=maximum_classification,
                    metadata={"decision_code": "invalid_manifest"},
                )
            if (
                _CLASSIFICATION_RANK[classification]
                > _CLASSIFICATION_RANK[maximum_classification]
            ):
                redacted_ids.append(content_id)

        reasons = (
            ("Sensitive prompt content was redacted",)
            if redacted_ids
            else ("Prompt content is allowed",)
        )
        return PolicyDecision(
            allowed=True,
            reasons=reasons,
            redacted_fields=tuple(redacted_ids),
            maximum_classification=maximum_classification,
            metadata={
                "decision_code": "allowed",
                "content_mode": "classified_items_only",
                **dict(metadata or {}),
            },
        )

    def evaluate_sql_access(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        tables: Sequence[str],
        columns: Sequence[str],
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            reasons=("SQL access policy is not configured in this implementation slice",),
        )


class SchemaQuestionPolicyEngine(MetadataOnlyPolicyEngine):
    def __init__(
        self,
        *,
        allowed_provider_ids: frozenset[str] = frozenset(),
        maximum_classification: Classification = Classification.INTERNAL,
        provider_policy_repository: ProviderPolicyRepository | None = None,
        provider_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        require_explicit_provider_policy: bool = False,
    ) -> None:
        super().__init__(
            allowed_provider_ids=allowed_provider_ids,
            maximum_classification=maximum_classification,
            allowed_content_kinds=(
                _SCHEMA_METADATA_KINDS
                | {
                    "business_concept",
                    "business_rule",
                    "corrected_sql_example",
                    "correction_constraint",
                    "correction_column_candidate",
                    "correction_table_candidate",
                    "current_intent_entity",
                    "generation_constraint",
                    "metric_definition",
                    "user_intent_correction",
                    "user_question",
                }
            ),
        )
        self._provider_policy_repository = provider_policy_repository
        self._provider_metadata = dict(provider_metadata or {})
        self._require_explicit_provider_policy = require_explicit_provider_policy

    def evaluate_prompt_egress(
        self,
        *,
        tenant_id: str,
        provider_id: str,
        content_manifest: Sequence[Mapping[str, Any]],
        data_source_id: str | None = None,
        purpose: str | None = None,
    ) -> PolicyDecision:
        policy = (
            self._provider_policy_repository.get_effective_provider_egress_policy(
                tenant_id,
                provider_id,
                data_source_id,
            )
            if self._provider_policy_repository is not None
            else None
        )
        if policy is None:
            if self._require_explicit_provider_policy:
                deployment = self._provider_metadata.get(provider_id, {})
                return PolicyDecision(
                    allowed=False,
                    reasons=("Explicit tenant provider policy is required",),
                    metadata={
                        "decision_code": "missing_policy",
                        "provider_policy_scope": "none",
                        "deployment_data_residency": deployment.get(
                            "data_residency"
                        ),
                        "deployment_retention_mode": deployment.get(
                            "retention_mode"
                        ),
                        "deployment_type": deployment.get("deployment_type"),
                        "deployment_model_id": deployment.get("model_id"),
                    },
                )
            return super().evaluate_prompt_egress(
                tenant_id=tenant_id,
                provider_id=provider_id,
                content_manifest=content_manifest,
                data_source_id=data_source_id,
                purpose=purpose,
            )
        deployment = self._provider_metadata.get(provider_id, {})
        if not policy.allowed:
            return PolicyDecision(
                allowed=False,
                reasons=(f"Provider {provider_id} is denied by tenant policy",),
                maximum_classification=policy.maximum_classification,
                metadata=self._policy_metadata(
                    policy,
                    "denied_provider",
                    deployment=deployment,
                ),
            )
        if purpose is None or purpose not in policy.allowed_purposes:
            return PolicyDecision(
                allowed=False,
                reasons=(f"Purpose {purpose or 'unknown'} is denied by tenant policy",),
                maximum_classification=policy.maximum_classification,
                metadata=self._policy_metadata(
                    policy,
                    "denied_purpose",
                    deployment=deployment,
                ),
            )
        residency = deployment.get("data_residency")
        retention = deployment.get("retention_mode")
        if residency != policy.data_residency:
            return PolicyDecision(
                allowed=False,
                reasons=("Provider data residency does not satisfy tenant policy",),
                maximum_classification=policy.maximum_classification,
                metadata=self._policy_metadata(
                    policy,
                    "residency_mismatch",
                    deployment=deployment,
                ),
            )
        if retention != policy.retention_mode.value:
            return PolicyDecision(
                allowed=False,
                reasons=("Provider retention mode does not satisfy tenant policy",),
                maximum_classification=policy.maximum_classification,
                metadata=self._policy_metadata(
                    policy,
                    "retention_mismatch",
                    deployment=deployment,
                ),
            )
        return self._evaluate_prompt_manifest(
            tenant_id=tenant_id,
            provider_id=provider_id,
            content_manifest=content_manifest,
            maximum_classification=policy.maximum_classification,
            metadata=self._policy_metadata(
                policy,
                "allowed",
                deployment=deployment,
            ),
        )

    @staticmethod
    def _policy_metadata(
        policy: ProviderEgressPolicy,
        decision_code: str,
        *,
        deployment: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        values: dict[str, Any] = {
            "decision_code": decision_code,
            "provider_policy_id": policy.id,
            "provider_policy_scope": (
                "data_source" if policy.data_source_id is not None else "tenant"
            ),
            "provider_policy_updated_at": policy.updated_at.isoformat(),
            "policy_allowed": policy.allowed,
            "data_residency": policy.data_residency,
            "retention_mode": policy.retention_mode.value,
            "allowed_purposes": policy.allowed_purposes,
            "acknowledgement_digest": policy.acknowledgement_digest,
        }
        if deployment is not None:
            values.update(
                {
                    "deployment_data_residency": deployment.get("data_residency"),
                    "deployment_retention_mode": deployment.get("retention_mode"),
                    "deployment_type": deployment.get("deployment_type"),
                    "model_id": deployment.get("model_id"),
                }
            )
        return values

    def evaluate_sql_access(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        tables: Sequence[str],
        columns: Sequence[str],
    ) -> PolicyDecision:
        if not tenant_id.strip() or not data_source_id.strip():
            return PolicyDecision(
                allowed=False,
                reasons=("Tenant and DataSource boundaries are required",),
            )
        if not tables or any(not table.strip() for table in tables):
            return PolicyDecision(
                allowed=False,
                reasons=("Validated SQL must reference at least one governed table",),
            )
        table_set = frozenset(tables)
        if any(
            not column.strip() or column.rsplit(".", 1)[0] not in table_set
            for column in columns
        ):
            return PolicyDecision(
                allowed=False,
                reasons=("SQL columns must belong to the validated table set",),
            )
        return PolicyDecision(
            allowed=True,
            reasons=("Validated non-redacted schema references are allowed",),
            metadata={"access_mode": "validated_context_only"},
        )
