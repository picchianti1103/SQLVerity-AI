from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any

from packages.connectors.sqlverity_connectors.connection import (
    SecretResolutionError,
    load_secret_resolver_from_environment,
)


class FakeVaultResponse:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Mapping[str, Any]:
        return self._payload


class RotatingVaultClient:
    def __init__(self, passwords: tuple[str, ...]) -> None:
        self._passwords = iter(passwords)
        self.requests: list[tuple[str, Mapping[str, str]]] = []
        self.closed = False

    def get(self, url: str, *, headers: Mapping[str, str]) -> FakeVaultResponse:
        self.requests.append((url, headers))
        return FakeVaultResponse(
            {
                "data": {
                    "data": {
                        "host": "db.internal",
                        "database": "analytics",
                        "username": "reader",
                        "password": next(self._passwords),
                        "sslmode": "verify-full",
                    }
                }
            }
        )

    def close(self) -> None:
        self.closed = True


class FakeAWSSecretsManagerClient:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.secret_ids: list[str] = []
        self.closed = False

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, str]:  # noqa: N803
        self.secret_ids.append(SecretId)
        return {"SecretString": json.dumps(self._payload)}

    def close(self) -> None:
        self.closed = True


class SecretManagerResolverTests(unittest.TestCase):
    def test_vault_kv_v2_is_fetched_fresh_to_support_rotation(self) -> None:
        vault = RotatingVaultClient(("first-password", "rotated-password"))
        resolver = load_secret_resolver_from_environment(
            {
                "SQLVERITY_SECRET_BACKENDS": "vault",
                "VAULT_ADDR": "https://vault.example.internal",
                "VAULT_TOKEN": "short-lived-token",
            },
            vault_client=vault,
        )

        first = resolver.resolve_postgresql("vault://secret/data/sqlverity/catalog")
        second = resolver.resolve_postgresql("vault://secret/data/sqlverity/catalog")
        resolver.close()

        self.assertEqual("first-password", first.password)
        self.assertEqual("rotated-password", second.password)
        self.assertEqual(2, len(vault.requests))
        self.assertTrue(vault.closed)
        self.assertEqual(
            "https://vault.example.internal/v1/secret/data/sqlverity/catalog",
            vault.requests[0][0],
        )

    def test_aws_secret_fragment_selects_database_payload(self) -> None:
        aws = FakeAWSSecretsManagerClient(
            {
                "catalog": {
                    "host": "catalog.internal",
                    "database": "sqlverity",
                    "username": "catalog_app",
                    "password": "managed-password",
                }
            }
        )
        resolver = load_secret_resolver_from_environment(
            {"SQLVERITY_SECRET_BACKENDS": "aws-secrets-manager"},
            aws_client=aws,
        )

        secret = resolver.resolve_postgresql(
            "aws-secretsmanager://prod/sqlverity#catalog"
        )

        self.assertEqual("catalog.internal", secret.host)
        self.assertEqual(["prod/sqlverity"], aws.secret_ids)

    def test_environment_references_fail_when_backend_is_not_enabled(self) -> None:
        resolver = load_secret_resolver_from_environment(
            {
                "SQLVERITY_SECRET_BACKENDS": "vault",
                "SQLVERITY_DB": '{"password":"must-not-leak"}',
            },
            vault_client=RotatingVaultClient(("unused",)),
        )

        with self.assertRaises(SecretResolutionError) as raised:
            resolver.resolve_postgresql("env://SQLVERITY_DB")

        self.assertNotIn("must-not-leak", str(raised.exception))

    def test_vault_rejects_plain_http_outside_explicit_loopback_development(self) -> None:
        resolver = load_secret_resolver_from_environment(
            {
                "SQLVERITY_SECRET_BACKENDS": "vault",
                "VAULT_ADDR": "http://vault.example.internal",
                "VAULT_TOKEN": "token",
            },
            vault_client=RotatingVaultClient(("unused",)),
        )

        with self.assertRaisesRegex(SecretResolutionError, "HTTPS"):
            resolver.resolve_postgresql("vault://secret/data/sqlverity")


if __name__ == "__main__":
    unittest.main()
