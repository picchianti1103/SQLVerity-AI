from __future__ import annotations

import unittest

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import (
    Classification,
    DataSourceType,
    ProviderEgressPolicy,
    ProviderRetentionMode,
)
from packages.llm_gateway.sqlverity_llm_gateway import SchemaQuestionPolicyEngine


class ProviderEgressPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = SQLiteCatalogRepository()
        self.repository.initialize()
        self.tenant = self.repository.create_tenant("Policy tenant")
        self.source = self.repository.create_data_source(
            tenant_id=self.tenant.id,
            name="Analytics",
            source_type=DataSourceType.DIRECT_DB,
            dialect="postgresql",
        )

    def tearDown(self) -> None:
        self.repository.close()

    def test_data_source_policy_overrides_tenant_policy_and_is_audited(self) -> None:
        tenant_policy = self.repository.upsert_provider_egress_policy(
            self._policy(maximum=Classification.INTERNAL)
        )
        source_policy = self.repository.upsert_provider_egress_policy(
            self._policy(
                data_source_id=self.source.id,
                maximum=Classification.CONFIDENTIAL,
            )
        )

        self.assertEqual(
            tenant_policy,
            self.repository.get_effective_provider_egress_policy(
                self.tenant.id,
                "openai",
                None,
            ),
        )
        self.assertEqual(
            source_policy,
            self.repository.get_effective_provider_egress_policy(
                self.tenant.id,
                "openai",
                self.source.id,
            ),
        )
        events = self.repository.audit_events(self.tenant.id)
        self.assertEqual(
            2,
            sum(event.event_type == "provider_egress_policy.upserted" for event in events),
        )

    def test_upsert_preserves_policy_identity(self) -> None:
        first = self.repository.upsert_provider_egress_policy(self._policy())
        second = self.repository.upsert_provider_egress_policy(
            self._policy(maximum=Classification.PII)
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(Classification.PII, second.maximum_classification)
        self.assertEqual(1, len(self.repository.list_provider_egress_policies(self.tenant.id)))

    def test_runtime_policy_enforces_purpose_residency_retention_and_classification(self) -> None:
        self.repository.upsert_provider_egress_policy(
            self._policy(maximum=Classification.CONFIDENTIAL)
        )
        engine = SchemaQuestionPolicyEngine(
            allowed_provider_ids=frozenset({"openai"}),
            provider_policy_repository=self.repository,
            provider_metadata={
                "openai": {"data_residency": "eu", "retention_mode": "zero"}
            },
            require_explicit_provider_policy=True,
        )
        manifest = (
            {
                "id": "public.orders",
                "kind": "schema_object",
                "classification": "internal",
            },
            {
                "id": "question",
                "kind": "user_question",
                "classification": "pii",
            },
        )

        allowed = engine.evaluate_prompt_egress(
            tenant_id=self.tenant.id,
            provider_id="openai",
            data_source_id=self.source.id,
            purpose="sql_proposal_generation",
            content_manifest=manifest,
        )
        denied_purpose = engine.evaluate_prompt_egress(
            tenant_id=self.tenant.id,
            provider_id="openai",
            data_source_id=self.source.id,
            purpose="unapproved_purpose",
            content_manifest=manifest,
        )

        self.assertTrue(allowed.allowed)
        self.assertEqual(("question",), allowed.redacted_fields)
        self.assertEqual("tenant", allowed.metadata["provider_policy_scope"])
        self.assertFalse(denied_purpose.allowed)
        self.assertEqual("denied_purpose", denied_purpose.metadata["decision_code"])

    def test_missing_explicit_policy_fails_closed(self) -> None:
        engine = SchemaQuestionPolicyEngine(
            allowed_provider_ids=frozenset({"openai"}),
            provider_policy_repository=self.repository,
            provider_metadata={
                "openai": {"data_residency": "eu", "retention_mode": "zero"}
            },
            require_explicit_provider_policy=True,
        )

        decision = engine.evaluate_prompt_egress(
            tenant_id=self.tenant.id,
            provider_id="openai",
            data_source_id=self.source.id,
            purpose="sql_proposal_generation",
            content_manifest=(),
        )

        self.assertFalse(decision.allowed)
        self.assertIn("required", decision.reasons[0])
        self.assertEqual("missing_policy", decision.metadata["decision_code"])
        self.assertEqual("none", decision.metadata["provider_policy_scope"])

    def test_deployment_residency_and_retention_mismatches_have_stable_codes(self) -> None:
        self.repository.upsert_provider_egress_policy(self._policy())
        manifest = (
            {
                "id": "public.orders",
                "kind": "schema_object",
                "classification": "internal",
            },
        )

        residency = SchemaQuestionPolicyEngine(
            allowed_provider_ids=frozenset({"openai"}),
            provider_policy_repository=self.repository,
            provider_metadata={
                "openai": {
                    "data_residency": "us",
                    "retention_mode": "zero",
                    "deployment_type": "external_cloud",
                    "model_id": "approved-model",
                }
            },
            require_explicit_provider_policy=True,
        ).evaluate_prompt_egress(
            tenant_id=self.tenant.id,
            provider_id="openai",
            data_source_id=self.source.id,
            purpose="sql_proposal_generation",
            content_manifest=manifest,
        )
        retention = SchemaQuestionPolicyEngine(
            allowed_provider_ids=frozenset({"openai"}),
            provider_policy_repository=self.repository,
            provider_metadata={
                "openai": {
                    "data_residency": "eu",
                    "retention_mode": "provider_default",
                    "deployment_type": "external_cloud",
                    "model_id": "approved-model",
                }
            },
            require_explicit_provider_policy=True,
        ).evaluate_prompt_egress(
            tenant_id=self.tenant.id,
            provider_id="openai",
            data_source_id=self.source.id,
            purpose="sql_proposal_generation",
            content_manifest=manifest,
        )

        self.assertFalse(residency.allowed)
        self.assertEqual("residency_mismatch", residency.metadata["decision_code"])
        self.assertEqual("us", residency.metadata["deployment_data_residency"])
        self.assertFalse(retention.allowed)
        self.assertEqual("retention_mismatch", retention.metadata["decision_code"])
        self.assertEqual(
            "provider_default",
            retention.metadata["deployment_retention_mode"],
        )

    def _policy(
        self,
        *,
        data_source_id: str | None = None,
        maximum: Classification = Classification.INTERNAL,
    ) -> ProviderEgressPolicy:
        return ProviderEgressPolicy(
            tenant_id=self.tenant.id,
            data_source_id=data_source_id,
            provider_id="openai",
            allowed_purposes=(
                "sql_proposal_generation",
                "semantic_description_inference",
            ),
            maximum_classification=maximum,
            data_residency="eu",
            retention_mode=ProviderRetentionMode.ZERO,
        )


if __name__ == "__main__":
    unittest.main()
