from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, cast
from urllib.parse import urlencode, urlparse


class OIDCConfigurationError(ValueError):
    pass


class OIDCAuthenticationError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    issuer: str
    audience: str
    jwks_url: str
    tenant_claim: str = "sqlverity_tenant_id"
    require_mfa: bool = True
    required_acr: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("issuer", self.issuer),
            ("audience", self.audience),
            ("jwks_url", self.jwks_url),
            ("tenant_claim", self.tenant_claim),
        ):
            if not value.strip():
                raise OIDCConfigurationError(f"OIDC {name} is required")
        for endpoint in (self.issuer, self.jwks_url):
            parsed = urlparse(endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise OIDCConfigurationError("OIDC endpoints must be HTTPS URLs")
        if self.required_acr is not None and not self.required_acr.strip():
            raise OIDCConfigurationError("OIDC required ACR must not be blank")


@dataclass(frozen=True, slots=True)
class FederatedIdentity:
    subject: str
    tenant_id: str
    display_name: str
    mfa_verified: bool


type OIDCDecoder = Callable[[str, OIDCSettings], Mapping[str, Any]]


class OIDCAuthenticator:
    def __init__(
        self,
        settings: OIDCSettings,
        *,
        decoder: OIDCDecoder | None = None,
    ) -> None:
        self._settings = settings
        self._decoder = decoder or _PyJWTDecoder(settings)

    def authenticate(
        self,
        token: str,
        *,
        expected_nonce: str | None = None,
    ) -> FederatedIdentity:
        if token.count(".") != 2:
            raise OIDCAuthenticationError("Bearer token is not an OIDC JWT")
        try:
            claims = self._decoder(token, self._settings)
        except OIDCAuthenticationError:
            raise
        except Exception as error:
            raise OIDCAuthenticationError("OIDC token validation failed") from error
        subject = claims.get("sub")
        tenant_id = claims.get(self._settings.tenant_claim)
        if not isinstance(subject, str) or not subject.strip():
            raise OIDCAuthenticationError("OIDC token has no valid subject")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise OIDCAuthenticationError("OIDC token has no valid tenant claim")
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise OIDCAuthenticationError("OIDC token nonce does not match the login flow")
        amr = claims.get("amr", ())
        methods = (
            frozenset(item.casefold() for item in amr if isinstance(item, str))
            if isinstance(amr, (list, tuple))
            else frozenset()
        )
        mfa_verified = bool(methods & {"mfa", "otp", "hwk", "swk"})
        acr = claims.get("acr")
        if self._settings.require_mfa and not mfa_verified:
            raise OIDCAuthenticationError("OIDC token does not prove MFA")
        if self._settings.required_acr is not None and acr != self._settings.required_acr:
            raise OIDCAuthenticationError("OIDC token does not satisfy the required ACR")
        display_name_value = claims.get("name") or claims.get("preferred_username") or subject
        display_name = (
            display_name_value.strip()
            if isinstance(display_name_value, str) and display_name_value.strip()
            else subject.strip()
        )
        return FederatedIdentity(
            subject=subject.strip(),
            tenant_id=tenant_id.strip(),
            display_name=display_name,
            mfa_verified=mfa_verified,
        )


@dataclass(frozen=True, slots=True)
class OIDCBrowserSettings:
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    session_secret: str = field(repr=False)
    client_secret: str | None = field(default=None, repr=False)
    secure_cookies: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("authorization endpoint", self.authorization_endpoint),
            ("token endpoint", self.token_endpoint),
            ("client id", self.client_id),
            ("redirect URI", self.redirect_uri),
        ):
            if not value.strip():
                raise OIDCConfigurationError(f"OIDC browser {name} is required")
        for endpoint in (self.authorization_endpoint, self.token_endpoint):
            _require_https_endpoint(endpoint, "OIDC browser endpoints")
        redirect = urlparse(self.redirect_uri)
        if (
            not redirect.hostname
            or redirect.username is not None
            or redirect.password is not None
            or redirect.fragment
            or redirect.query
            or (
                redirect.scheme != "https"
                and not (
                    redirect.scheme == "http"
                    and redirect.hostname in {"127.0.0.1", "localhost", "::1"}
                    and not self.secure_cookies
                )
            )
        ):
            raise OIDCConfigurationError("OIDC browser redirect URI is invalid")
        if len(self.session_secret) < 32:
            raise OIDCConfigurationError(
                "SQLVERITY_BROWSER_SESSION_SECRET must contain at least 32 characters"
            )


@dataclass(frozen=True, slots=True)
class OIDCLoginRequest:
    authorization_url: str
    flow_cookie: str


class OIDCBrowserFlow:
    def __init__(
        self,
        settings: OIDCBrowserSettings,
        authenticator: OIDCAuthenticator,
        *,
        http_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._authenticator = authenticator
        self._http_client = http_client
        self._clock = clock

    def begin_login(self) -> OIDCLoginRequest:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        flow_cookie = self._seal(
            {
                "state": state,
                "nonce": nonce,
                "verifier": verifier,
                "expires_at": int(self._clock()) + 600,
            }
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return OIDCLoginRequest(
            authorization_url=f"{self.settings.authorization_endpoint}?{query}",
            flow_cookie=flow_cookie,
        )

    def exchange_callback(self, *, code: str, state: str, flow_cookie: str) -> str:
        if not code.strip() or not state.strip():
            raise OIDCAuthenticationError("OIDC callback is incomplete")
        flow = self._unseal(flow_cookie)
        if flow.get("state") != state:
            raise OIDCAuthenticationError("OIDC callback state does not match")
        expires_at = flow.get("expires_at")
        verifier = flow.get("verifier")
        nonce = flow.get("nonce")
        if (
            not isinstance(expires_at, int)
            or expires_at < int(self._clock())
            or not isinstance(verifier, str)
            or not isinstance(nonce, str)
        ):
            raise OIDCAuthenticationError("OIDC login flow has expired")
        client = self._http_client
        if client is None:
            try:
                httpx_module = import_module("httpx")
                client = httpx_module.Client(timeout=15.0)
            except (AttributeError, ImportError) as error:
                raise OIDCConfigurationError("httpx is required for OIDC login") from error
            self._http_client = client
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": verifier,
        }
        if self.settings.client_secret is not None:
            form["client_secret"] = self.settings.client_secret
        try:
            response = client.post(self.settings.token_endpoint, data=form)
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise OIDCAuthenticationError("OIDC token exchange failed") from error
        if not isinstance(payload, Mapping):
            raise OIDCAuthenticationError("OIDC token response is invalid")
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or len(id_token) > 16_384:
            raise OIDCAuthenticationError("OIDC token response has no valid ID token")
        self._authenticator.authenticate(id_token, expected_nonce=nonce)
        return id_token

    def close(self) -> None:
        client = self._http_client
        close = getattr(client, "close", None) if client is not None else None
        if callable(close):
            close()
        self._http_client = None

    def _seal(self, payload: Mapping[str, object]) -> str:
        encoded = _base64url(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = _base64url(
            hmac.new(
                self.settings.session_secret.encode("utf-8"),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return f"{encoded}.{signature}"

    def _unseal(self, value: str) -> Mapping[str, Any]:
        encoded, separator, signature = value.partition(".")
        if not separator:
            raise OIDCAuthenticationError("OIDC login cookie is invalid")
        expected = _base64url(
            hmac.new(
                self.settings.session_secret.encode("utf-8"),
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise OIDCAuthenticationError("OIDC login cookie is invalid")
        try:
            payload = json.loads(_base64url_decode(encoded))
        except (UnicodeDecodeError, ValueError) as error:
            raise OIDCAuthenticationError("OIDC login cookie is invalid") from error
        if not isinstance(payload, Mapping):
            raise OIDCAuthenticationError("OIDC login cookie is invalid")
        return payload


class _PyJWTDecoder:
    def __init__(self, settings: OIDCSettings) -> None:
        try:
            jwt_module = import_module("jwt")
            self._jwt = jwt_module
            self._jwks_client = jwt_module.PyJWKClient(
                settings.jwks_url,
                cache_keys=True,
                lifespan=300,
            )
        except (AttributeError, ImportError) as error:
            raise OIDCConfigurationError(
                "The identity extra is required when OIDC is enabled"
            ) from error

    def __call__(self, token: str, settings: OIDCSettings) -> Mapping[str, Any]:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = self._jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=settings.audience,
                issuer=settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as error:
            raise OIDCAuthenticationError("OIDC token validation failed") from error
        if not isinstance(claims, Mapping):
            raise OIDCAuthenticationError("OIDC claims are invalid")
        return cast(Mapping[str, Any], claims)


def load_oidc_authenticator_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    decoder: OIDCDecoder | None = None,
) -> OIDCAuthenticator | None:
    environment = os.environ if environ is None else environ
    issuer = environment.get("SQLVERITY_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    settings = OIDCSettings(
        issuer=issuer,
        audience=environment.get("SQLVERITY_OIDC_AUDIENCE", ""),
        jwks_url=environment.get("SQLVERITY_OIDC_JWKS_URL", ""),
        tenant_claim=environment.get("SQLVERITY_OIDC_TENANT_CLAIM", "sqlverity_tenant_id"),
        require_mfa=_environment_bool(environment, "SQLVERITY_OIDC_REQUIRE_MFA", True),
        required_acr=environment.get("SQLVERITY_OIDC_REQUIRED_ACR") or None,
    )
    return OIDCAuthenticator(settings, decoder=decoder)


def load_oidc_browser_flow_from_environment(
    authenticator: OIDCAuthenticator | None,
    environ: Mapping[str, str] | None = None,
    *,
    http_client: Any | None = None,
    clock: Callable[[], float] = time.time,
) -> OIDCBrowserFlow | None:
    environment = os.environ if environ is None else environ
    client_id = environment.get("SQLVERITY_OIDC_CLIENT_ID", "").strip()
    browser_values = (
        client_id,
        environment.get("SQLVERITY_OIDC_AUTHORIZATION_ENDPOINT", "").strip(),
        environment.get("SQLVERITY_OIDC_TOKEN_ENDPOINT", "").strip(),
        environment.get("SQLVERITY_OIDC_REDIRECT_URI", "").strip(),
        environment.get("SQLVERITY_BROWSER_SESSION_SECRET", ""),
    )
    if not any(browser_values):
        return None
    if authenticator is None:
        raise OIDCConfigurationError("OIDC token validation must be enabled for browser login")
    if not all(browser_values):
        raise OIDCConfigurationError("OIDC browser login configuration is incomplete")
    secure_cookies = _environment_bool(
        environment,
        "SQLVERITY_BROWSER_SECURE_COOKIES",
        True,
    )
    settings = OIDCBrowserSettings(
        authorization_endpoint=browser_values[1],
        token_endpoint=browser_values[2],
        client_id=client_id,
        redirect_uri=browser_values[3],
        session_secret=browser_values[4],
        client_secret=environment.get("SQLVERITY_OIDC_CLIENT_SECRET") or None,
        secure_cookies=secure_cookies,
    )
    return OIDCBrowserFlow(
        settings,
        authenticator,
        http_client=http_client,
        clock=clock,
    )


def _environment_bool(
    environ: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise OIDCConfigurationError(f"{name} must be a boolean")


def _require_https_endpoint(value: str, label: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
    ):
        raise OIDCConfigurationError(f"{label} must be HTTPS URLs without credentials")


def _base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return urlsafe_b64decode(value + padding)
