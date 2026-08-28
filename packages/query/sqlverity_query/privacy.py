from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Protocol

from packages.domain.sqlverity_domain.models import (
    AIContentManifestCount,
    AITransferReceipt,
    Classification,
)
from packages.llm_gateway.sqlverity_llm_gateway import LLMPreflightResult


class PreflightConfirmationError(RuntimeError):
    code = "stale_preflight"


class AITransferReceiptRecorder(Protocol):
    def record_ai_transfer_receipt(
        self,
        receipt: AITransferReceipt,
    ) -> AITransferReceipt: ...


class PreflightConfirmationStore(Protocol):
    def register_preflight_confirmation(
        self,
        token_id: str,
        expires_at: datetime,
    ) -> None: ...

    def consume_preflight_confirmation(
        self,
        token_id: str,
        consumed_at: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class SQLGenerationPreflight:
    provider_id: str
    model_id: str
    purpose: str
    data_source_id: str
    catalog_version_id: str
    policy_id: str | None
    policy_scope: str
    policy_version: str | None
    maximum_allowed_classification: Classification
    declared_classification: Classification
    detected_classification: Classification
    effective_classification: Classification
    detection_reason_codes: tuple[str, ...]
    data_residency: str
    retention_mode: str
    deployment_type: str
    allowed: bool
    decision_code: str
    review_required: bool
    content_counts: tuple[AIContentManifestCount, ...]
    included_content_ids: tuple[str, ...]
    redacted_content_ids: tuple[str, ...]
    semantic_retry_possible: bool
    maximum_provider_calls: int
    provider_invoked: bool
    manifest_digest: str
    question_digest: str
    confirmation_token: str | None
    confirmation_expires_at: datetime | None
    receipt_id: str | None = None


class PreflightConfirmationManager:
    """Short-lived HMAC confirmation with optional shared single-use storage."""

    def __init__(
        self,
        signing_key: bytes | None = None,
        *,
        ttl_seconds: int = 300,
        confirmation_store: PreflightConfirmationStore | None = None,
    ) -> None:
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("Preflight confirmation TTL must be between 30 and 3600 seconds")
        self._signing_key = signing_key or secrets.token_bytes(32)
        if len(self._signing_key) < 32:
            raise ValueError("Preflight signing key must contain at least 32 bytes")
        self._ttl_seconds = ttl_seconds
        self._confirmation_store = confirmation_store
        self._used: dict[str, int] = {}
        self._lock = RLock()

    def issue(self, binding: Mapping[str, Any]) -> tuple[str, datetime]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        token_id = secrets.token_hex(16)
        payload = {
            "v": 1,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": token_id,
            "binding": dict(binding),
        }
        if self._confirmation_store is not None:
            self._confirmation_store.register_preflight_confirmation(
                token_id,
                expires_at,
            )
        encoded = _base64url(_canonical_json(payload))
        signature = _base64url(hmac.digest(self._signing_key, encoded, "sha256"))
        return f"{encoded.decode('ascii')}.{signature.decode('ascii')}", expires_at

    def consume(self, token: str, expected_binding: Mapping[str, Any]) -> None:
        try:
            encoded_text, signature_text = token.split(".", 1)
            encoded = encoded_text.encode("ascii")
            supplied_signature = _base64url_decode(signature_text)
            expected_signature = hmac.digest(self._signing_key, encoded, "sha256")
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise PreflightConfirmationError("Preflight confirmation signature is invalid")
            payload = json.loads(_base64url_decode(encoded_text))
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise PreflightConfirmationError("Preflight confirmation is invalid") from error
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise PreflightConfirmationError("Preflight confirmation version is invalid")
        expires_at = payload.get("exp")
        issued_at = payload.get("iat")
        token_id = payload.get("jti")
        if not isinstance(expires_at, int) or not isinstance(issued_at, int):
            raise PreflightConfirmationError("Preflight confirmation timestamps are invalid")
        if not isinstance(token_id, str) or not token_id:
            raise PreflightConfirmationError("Preflight confirmation id is invalid")
        now_datetime = datetime.now(UTC)
        now = int(now_datetime.timestamp())
        if issued_at > now + 30 or expires_at < now:
            raise PreflightConfirmationError("Preflight confirmation has expired")
        if payload.get("binding") != dict(expected_binding):
            raise PreflightConfirmationError(
                "Preflight confirmation no longer matches the current request"
            )
        if self._confirmation_store is not None:
            if not self._confirmation_store.consume_preflight_confirmation(
                token_id,
                now_datetime,
            ):
                raise PreflightConfirmationError(
                    "Preflight confirmation was already used or expired"
                )
            return
        with self._lock:
            self._used = {
                current_id: expiry
                for current_id, expiry in self._used.items()
                if expiry >= now
            }
            if token_id in self._used:
                raise PreflightConfirmationError("Preflight confirmation was already used")
            self._used[token_id] = expires_at


def preflight_binding(
    *,
    tenant_id: str,
    actor_id: str,
    data_source_id: str,
    provider_id: str,
    model_id: str,
    purpose: str,
    catalog_version_id: str,
    policy_id: str | None,
    policy_version: str | None,
    question_digest: str,
    privacy_mode: str,
    manifest_digest: str,
) -> Mapping[str, Any]:
    return {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "data_source_id": data_source_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "purpose": purpose,
        "catalog_version_id": catalog_version_id,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "question_digest": question_digest,
        "privacy_mode": privacy_mode,
        "manifest_digest": manifest_digest,
    }


def question_digest(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def policy_acknowledgement_digest(
    *,
    provider_id: str,
    model_id: str,
    allowed: bool,
    allowed_purposes: Sequence[str],
    maximum_classification: Classification,
    data_residency: str,
    retention_mode: str,
    scope: str,
    deployment_type: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "provider_id": provider_id,
                "model_id": model_id,
                "allowed": allowed,
                "allowed_purposes": sorted(allowed_purposes),
                "maximum_classification": maximum_classification.value,
                "data_residency": data_residency,
                "retention_mode": retention_mode,
                "scope": scope,
                "deployment_type": deployment_type,
            }
        )
    ).hexdigest()


def summarize_manifest(
    preflight: LLMPreflightResult,
) -> tuple[AIContentManifestCount, ...]:
    counts: Counter[tuple[str, Classification, bool]] = Counter()
    redacted = preflight.redacted_content_ids
    for item in preflight.content_manifest:
        kind = item.get("kind")
        classification = item.get("classification")
        content_id = item.get("id")
        if not isinstance(kind, str) or not isinstance(classification, str):
            continue
        try:
            level = Classification(classification)
        except ValueError:
            continue
        counts[(kind, level, content_id in redacted)] += 1
    pairs = sorted({(kind, level) for kind, level, _redacted in counts})
    return tuple(
        AIContentManifestCount(
            kind=kind,
            classification=level,
            included_count=counts[(kind, level, False)],
            redacted_count=counts[(kind, level, True)],
        )
        for kind, level in pairs
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _base64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))
