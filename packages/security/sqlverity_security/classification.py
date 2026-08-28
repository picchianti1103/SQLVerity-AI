from __future__ import annotations

import re
from dataclasses import dataclass

from packages.domain.sqlverity_domain.models import Classification

_CLASSIFICATION_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.PII: 3,
    Classification.HIGHLY_SENSITIVE: 4,
}

_HIGHLY_SENSITIVE_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|secret)\b"
            r"\s*[:=]\s*[^\s,;]{6,}"
        ),
    ),
    ("payment_card", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
)

_PII_PATTERNS = (
    (
        "email",
        re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+"),
    ),
    (
        "iban",
        re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}(?![A-Z0-9])"),
    ),
    (
        "italian_tax_code",
        re.compile(r"(?i)(?<![A-Z0-9])[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z](?![A-Z0-9])"),
    ),
    (
        "phone_number",
        re.compile(r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\d[ .-]?){8,14}(?!\w)"),
    ),
)

_CONFIDENTIAL_PATTERNS = (
    (
        "confidential_business_data",
        re.compile(
            r"(?i)\b(?:salary|stipendio|diagnosis|diagnosi|medical record|"
            r"cartella clinica|bank account|conto corrente)\b"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ClassificationAssessment:
    declared: Classification
    detected: Classification
    effective: Classification
    reasons: tuple[str, ...]


class ServerSideTextClassifier:
    """Conservative deterministic DLP floor; the client can only make it stricter."""

    def classify(
        self,
        text: str,
        declared: Classification,
    ) -> ClassificationAssessment:
        detected, reasons = self._detect(text)
        effective = max(
            (declared, detected),
            key=lambda classification: _CLASSIFICATION_RANK[classification],
        )
        return ClassificationAssessment(
            declared=declared,
            detected=detected,
            effective=effective,
            reasons=reasons,
        )

    @staticmethod
    def _detect(text: str) -> tuple[Classification, tuple[str, ...]]:
        for classification, patterns in (
            (Classification.HIGHLY_SENSITIVE, _HIGHLY_SENSITIVE_PATTERNS),
            (Classification.PII, _PII_PATTERNS),
            (Classification.CONFIDENTIAL, _CONFIDENTIAL_PATTERNS),
        ):
            reasons = tuple(name for name, pattern in patterns if pattern.search(text))
            if reasons:
                return classification, reasons
        return Classification.INTERNAL, ("server_default_internal",)
