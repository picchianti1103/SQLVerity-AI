from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import PlatformRole
from packages.security.sqlverity_security import (
    AuthenticationService,
    OIDCAuthenticationError,
    OIDCAuthenticator,
    OIDCBrowserFlow,
    OIDCBrowserSettings,
    OIDCSettings,
    load_oidc_authenticator_from_environment,
)


class FakeTokenResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> Mapping[str, str]:
        return {"id_token": "header.payload.signature"}


class FakeTokenClient:
    def __init__(self) -> None:
        self.forms: list[Mapping[str, str]] = []
        self.closed = False

    def post(self, _url: str, *, data: Mapping[str, str]) -> FakeTokenResponse:
        self.forms.append(dict(data))
        return FakeTokenResponse()

    def close(self) -> None:
        self.closed = True


class OIDCTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = OIDCSettings(
            issuer="https://identity.example.com/realms/sqlverity",
            audience="sqlverity-api",
            jwks_url="https://identity.example.com/realms/sqlverity/jwks",
        )

    def test_valid_mfa_identity_maps_to_preprovisioned_principal(self) -> None:
        repository = SQLiteCatalogRepository()
        repository.initialize()
        try:
            tenant = repository.create_tenant("Federated tenant")
            authenticator = OIDCAuthenticator(
                self.settings,
                decoder=lambda _token, _settings: {
                    "sub": "idp-user-42",
                    "sqlverity_tenant_id": tenant.id,
                    "name": "Ada Admin",
                    "amr": ["pwd", "mfa"],
                },
            )
            security = AuthenticationService(
                repository,
                "bootstrap-key-with-at-least-thirty-two-characters",
                authenticator,
            )
            issued = security.provision_federated_principal(
                tenant_id=tenant.id,
                subject="idp-user-42",
                display_name="Ada Admin",
                role=PlatformRole.ADMIN,
                created_by="bootstrap-admin",
            )

            principal = security.authenticate_bearer("Bearer header.payload.signature")

            self.assertEqual(issued.principal.id, principal.id)
            self.assertEqual("oidc", principal.authentication_method)
            self.assertTrue(principal.mfa_verified)
            self.assertIsNone(principal.credential_id)
            self.assertEqual((), issued.credentials)
        finally:
            repository.close()

    def test_missing_mfa_is_rejected(self) -> None:
        authenticator = OIDCAuthenticator(
            self.settings,
            decoder=lambda _token, _settings: {
                "sub": "user",
                "sqlverity_tenant_id": "tenant",
                "amr": ["pwd"],
            },
        )

        with self.assertRaisesRegex(OIDCAuthenticationError, "MFA"):
            authenticator.authenticate("header.payload.signature")

    def test_required_acr_is_enforced(self) -> None:
        settings = OIDCSettings(
            issuer=self.settings.issuer,
            audience=self.settings.audience,
            jwks_url=self.settings.jwks_url,
            require_mfa=False,
            required_acr="urn:example:assurance:high",
        )

        def decoder(_token: str, _settings: OIDCSettings) -> Mapping[str, Any]:
            return {
                "sub": "user",
                "sqlverity_tenant_id": "tenant",
                "acr": "urn:example:assurance:low",
            }

        with self.assertRaisesRegex(OIDCAuthenticationError, "ACR"):
            OIDCAuthenticator(settings, decoder=decoder).authenticate(
                "header.payload.signature"
            )

    def test_incomplete_or_insecure_environment_configuration_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "audience"):
            load_oidc_authenticator_from_environment(
                {
                    "SQLVERITY_OIDC_ISSUER": "https://identity.example.com",
                    "SQLVERITY_OIDC_JWKS_URL": "https://identity.example.com/jwks",
                },
                decoder=lambda _token, _settings: {},
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            OIDCSettings(
                issuer="http://identity.example.com",
                audience="sqlverity",
                jwks_url="https://identity.example.com/jwks",
            )

    def test_browser_flow_uses_pkce_signed_state_and_nonce(self) -> None:
        nonce = ""

        def decoder(_token: str, _settings: OIDCSettings) -> Mapping[str, Any]:
            return {
                "sub": "user",
                "sqlverity_tenant_id": "tenant",
                "amr": ["mfa"],
                "nonce": nonce,
            }

        authenticator = OIDCAuthenticator(self.settings, decoder=decoder)
        token_client = FakeTokenClient()
        flow = OIDCBrowserFlow(
            OIDCBrowserSettings(
                authorization_endpoint="https://identity.example.com/authorize",
                token_endpoint="https://identity.example.com/token",
                client_id="sqlverity-console",
                redirect_uri="https://sqlverity.example.com/auth/oidc/callback",
                session_secret="browser-session-secret-with-at-least-32-characters",
            ),
            authenticator,
            http_client=token_client,
            clock=lambda: 1000.0,
        )

        login = flow.begin_login()
        query = parse_qs(urlparse(login.authorization_url).query)
        nonce = query["nonce"][0]
        token = flow.exchange_callback(
            code="authorization-code",
            state=query["state"][0],
            flow_cookie=login.flow_cookie,
        )
        flow.close()

        self.assertEqual("header.payload.signature", token)
        self.assertEqual("S256", query["code_challenge_method"][0])
        self.assertNotEqual(
            query["code_challenge"][0],
            token_client.forms[0]["code_verifier"],
        )
        self.assertNotIn("client_secret", token_client.forms[0])
        self.assertTrue(token_client.closed)

    def test_browser_flow_rejects_tampered_state_cookie(self) -> None:
        authenticator = OIDCAuthenticator(
            self.settings,
            decoder=lambda _token, _settings: {},
        )
        flow = OIDCBrowserFlow(
            OIDCBrowserSettings(
                authorization_endpoint="https://identity.example.com/authorize",
                token_endpoint="https://identity.example.com/token",
                client_id="sqlverity-console",
                redirect_uri="https://sqlverity.example.com/auth/oidc/callback",
                session_secret="browser-session-secret-with-at-least-32-characters",
            ),
            authenticator,
            http_client=FakeTokenClient(),
        )
        login = flow.begin_login()
        state = parse_qs(urlparse(login.authorization_url).query)["state"][0]

        with self.assertRaisesRegex(OIDCAuthenticationError, "cookie"):
            flow.exchange_callback(
                code="code",
                state=state,
                flow_cookie=f"{login.flow_cookie}tampered",
            )


if __name__ == "__main__":
    unittest.main()
