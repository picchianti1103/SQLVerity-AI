from __future__ import annotations

import unittest

from packages.domain.sqlverity_domain.models import Classification
from packages.security.sqlverity_security import ServerSideTextClassifier


class ServerSideTextClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = ServerSideTextClassifier()

    def test_client_cannot_downgrade_detected_email(self) -> None:
        result = self.classifier.classify(
            "Mostra gli ordini di mario.rossi@example.com",
            Classification.INTERNAL,
        )

        self.assertEqual(Classification.PII, result.detected)
        self.assertEqual(Classification.PII, result.effective)
        self.assertEqual(("email",), result.reasons)

    def test_secret_assignment_is_highly_sensitive(self) -> None:
        result = self.classifier.classify(
            "debug con api_key=sk-production-secret-value",
            Classification.PUBLIC,
        )

        self.assertEqual(Classification.HIGHLY_SENSITIVE, result.effective)
        self.assertIn("credential_assignment", result.reasons)

    def test_stricter_client_classification_is_preserved(self) -> None:
        result = self.classifier.classify(
            "Mostra il totale degli ordini",
            Classification.CONFIDENTIAL,
        )

        self.assertEqual(Classification.INTERNAL, result.detected)
        self.assertEqual(Classification.CONFIDENTIAL, result.effective)


if __name__ == "__main__":
    unittest.main()
