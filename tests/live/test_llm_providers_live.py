from __future__ import annotations

import os
import unittest

from packages.llm_gateway.sqlverity_llm_gateway import (
    load_llm_providers_from_environment,
)

_RUN_LIVE = os.environ.get("SQLVERITY_RUN_LIVE_PROVIDER_TESTS", "").casefold() == "true"


@unittest.skipUnless(_RUN_LIVE, "Live provider calls are not explicitly enabled")
class LLMProvidersLiveTests(unittest.TestCase):
    def test_every_selected_provider_returns_real_structured_usage(self) -> None:
        providers = load_llm_providers_from_environment()
        self.assertTrue(providers, "Select at least one live provider")
        request = {
            "purpose": "live_provider_certification",
            "instructions": "Return the requested constant and no other content.",
            "input": {"trust_level": "untrusted_data", "constant": "ok"},
            "output_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string", "const": "ok"}},
            },
        }

        for provider_id, provider in providers.items():
            with self.subTest(provider=provider_id):
                response = provider.generate_structured(request)
                self.assertEqual("ok", response.payload["status"])
                self.assertGreater(response.input_tokens, 0)
                self.assertGreater(response.output_tokens, 0)
                self.assertGreaterEqual(response.latency_ms, 0)
