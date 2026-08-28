from __future__ import annotations

import json
import os
import re
from base64 import b64decode
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Protocol, cast
from urllib.parse import unquote, urlparse


class ConnectorConfigurationError(ValueError):
    pass


class ConnectorUnavailableError(RuntimeError):
    pass


class SecretResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PostgreSQLConnectionSecret:
    host: str
    database: str
    username: str
    password: str = field(repr=False)
    port: int = 5432
    sslmode: str = "require"
    connect_timeout_seconds: int = 10

    def as_connect_kwargs(
        self,
        *,
        application_name: str = "sqlverity-introspection",
    ) -> dict[str, str | int]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.username,
            "password": self.password,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout_seconds,
            "application_name": application_name,
        }


@dataclass(frozen=True, slots=True)
class MySQLConnectionSecret:
    host: str
    database: str
    username: str
    password: str = field(repr=False)
    port: int = 3306
    connect_timeout_seconds: int = 10
    tls_required: bool = True
    ssl_ca: str | None = None

    def as_connect_kwargs(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.username,
            "password": self.password,
            "connection_timeout": self.connect_timeout_seconds,
            "autocommit": False,
            "charset": "utf8mb4",
            "use_unicode": True,
            "ssl_disabled": not self.tls_required,
        }
        if self.ssl_ca:
            result.update(
                {
                    "ssl_ca": self.ssl_ca,
                    "ssl_verify_cert": True,
                    "ssl_verify_identity": True,
                }
            )
        return result

    def as_mariadb_connect_kwargs(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.username,
            "password": self.password,
            "connect_timeout": self.connect_timeout_seconds,
            "autocommit": False,
            "ssl": self.tls_required,
        }
        if self.ssl_ca:
            result.update({"ssl_ca": self.ssl_ca, "ssl_verify_cert": True})
        return result


@dataclass(frozen=True, slots=True)
class OracleConnectionSecret:
    host: str
    service_name: str
    username: str
    password: str = field(repr=False)
    port: int = 1521
    connect_timeout_seconds: int = 10
    tls_required: bool = True
    wallet_location: str | None = None
    wallet_password: str | None = field(default=None, repr=False)

    def as_connect_kwargs(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "service_name": self.service_name,
            "user": self.username,
            "password": self.password,
            "protocol": "tcps" if self.tls_required else "tcp",
            "tcp_connect_timeout": float(self.connect_timeout_seconds),
        }
        if self.tls_required:
            result["ssl_server_dn_match"] = True
        if self.wallet_location is not None:
            result["wallet_location"] = self.wallet_location
        if self.wallet_password is not None:
            result["wallet_password"] = self.wallet_password
        return result


@dataclass(frozen=True, slots=True)
class SQLServerConnectionSecret:
    host: str
    database: str
    username: str
    password: str = field(repr=False)
    port: int = 1433
    connect_timeout_seconds: int = 10
    encrypt: bool = True
    trust_server_certificate: bool = False

    def as_connect_kwargs(self) -> dict[str, Any]:
        return {
            "server": f"{self.host},{self.port}",
            "database": self.database,
            "uid": self.username,
            "pwd": self.password,
            "encrypt": "yes" if self.encrypt else "no",
            "trust_server_certificate": (
                "yes" if self.trust_server_certificate else "no"
            ),
            "applicationintent": "ReadOnly",
            "timeout": self.connect_timeout_seconds,
        }

class SecretResolver(Protocol):
    def resolve_postgresql(self, secret_ref: str) -> PostgreSQLConnectionSecret: ...


class MySQLSecretResolver(Protocol):
    def resolve_mysql(self, secret_ref: str) -> MySQLConnectionSecret: ...


class OracleSecretResolver(Protocol):
    def resolve_oracle(self, secret_ref: str) -> OracleConnectionSecret: ...


class SQLServerSecretResolver(Protocol):
    def resolve_sqlserver(self, secret_ref: str) -> SQLServerConnectionSecret: ...


class DatabaseSecretResolver(
    SecretResolver,
    MySQLSecretResolver,
    OracleSecretResolver,
    SQLServerSecretResolver,
    Protocol,
):
    pass


class EnvironmentSecretResolver:
    """Development-only resolver for `env://VARIABLE_NAME` secret references."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve_postgresql(self, secret_ref: str) -> PostgreSQLConnectionSecret:
        payload, variable_name = self._payload(secret_ref)
        try:
            return _postgresql_secret(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_payload(variable_name) from error

    def resolve_mysql(self, secret_ref: str) -> MySQLConnectionSecret:
        payload, variable_name = self._payload(secret_ref)
        try:
            return _mysql_secret(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_payload(variable_name) from error

    def resolve_oracle(self, secret_ref: str) -> OracleConnectionSecret:
        payload, variable_name = self._payload(secret_ref)
        try:
            return _oracle_secret(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_payload(variable_name) from error

    def resolve_sqlserver(self, secret_ref: str) -> SQLServerConnectionSecret:
        payload, variable_name = self._payload(secret_ref)
        try:
            return _sqlserver_secret(payload)
        except (KeyError, TypeError, ValueError) as error:
            raise self._invalid_payload(variable_name) from error

    def _payload(self, secret_ref: str) -> tuple[dict[str, Any], str]:
        prefix = "env://"
        if not secret_ref.startswith(prefix):
            raise SecretResolutionError("Environment resolver requires an env:// reference")
        variable_name = secret_ref.removeprefix(prefix)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable_name):
            raise SecretResolutionError("Invalid environment secret variable name")
        raw_value = self._environ.get(variable_name)
        if raw_value is None:
            raise SecretResolutionError(f"Secret environment variable {variable_name} is missing")
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise self._invalid_payload(variable_name) from error
        if not isinstance(payload, dict):
            raise self._invalid_payload(variable_name)
        return payload, variable_name

    @staticmethod
    def _invalid_payload(variable_name: str) -> SecretResolutionError:
        return SecretResolutionError(
            f"Secret environment variable {variable_name} has an invalid JSON payload"
        )


class SecretManagerResolver:
    """Resolve fresh JSON secrets from explicitly enabled environment, Vault, or AWS backends."""

    def __init__(
        self,
        *,
        enabled_backends: frozenset[str],
        environ: Mapping[str, str] | None = None,
        vault_client: Any | None = None,
        aws_client: Any | None = None,
    ) -> None:
        if not enabled_backends:
            raise ValueError("At least one secret backend must be enabled")
        supported = {"environment", "vault", "aws-secrets-manager"}
        if not enabled_backends.issubset(supported):
            unknown = ", ".join(sorted(enabled_backends - supported))
            raise ValueError(f"Unsupported secret backend: {unknown}")
        self._enabled_backends = enabled_backends
        self._environ = environ if environ is not None else os.environ
        self._vault_client = vault_client
        self._aws_client = aws_client

    def resolve_postgresql(self, secret_ref: str) -> PostgreSQLConnectionSecret:
        try:
            return _postgresql_secret(self._payload(secret_ref))
        except (KeyError, TypeError, ValueError) as error:
            raise SecretResolutionError(
                "Managed PostgreSQL secret has an invalid payload"
            ) from error

    def resolve_mysql(self, secret_ref: str) -> MySQLConnectionSecret:
        try:
            return _mysql_secret(self._payload(secret_ref))
        except (KeyError, TypeError, ValueError) as error:
            raise SecretResolutionError("Managed MySQL secret has an invalid payload") from error

    def resolve_oracle(self, secret_ref: str) -> OracleConnectionSecret:
        try:
            return _oracle_secret(self._payload(secret_ref))
        except (KeyError, TypeError, ValueError) as error:
            raise SecretResolutionError("Managed Oracle secret has an invalid payload") from error

    def resolve_sqlserver(self, secret_ref: str) -> SQLServerConnectionSecret:
        try:
            return _sqlserver_secret(self._payload(secret_ref))
        except (KeyError, TypeError, ValueError) as error:
            raise SecretResolutionError(
                "Managed SQL Server secret has an invalid payload"
            ) from error

    def close(self) -> None:
        for client in (self._vault_client, self._aws_client):
            close = getattr(client, "close", None) if client is not None else None
            if callable(close):
                close()
        self._vault_client = None
        self._aws_client = None

    def _payload(self, secret_ref: str) -> dict[str, Any]:
        parsed = urlparse(secret_ref)
        scheme = parsed.scheme.casefold()
        if scheme == "env":
            self._require_backend("environment")
            return EnvironmentSecretResolver(self._environ)._payload(secret_ref)[0]
        if scheme == "vault":
            self._require_backend("vault")
            return self._vault_payload(parsed)
        if scheme == "aws-secretsmanager":
            self._require_backend("aws-secrets-manager")
            return self._aws_payload(parsed)
        raise SecretResolutionError("Unsupported secret reference scheme")

    def _require_backend(self, backend: str) -> None:
        if backend not in self._enabled_backends:
            raise SecretResolutionError(f"Secret backend {backend} is not enabled")

    def _vault_payload(self, parsed: Any) -> dict[str, Any]:
        path = f"{parsed.netloc}{parsed.path}".strip("/")
        if not path or ".." in path.split("/") or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
            raise SecretResolutionError("Invalid Vault secret path")
        address = self._environ.get("VAULT_ADDR", "").strip().rstrip("/")
        token = self._environ.get("VAULT_TOKEN", "").strip()
        if not address or not token:
            raise SecretResolutionError("VAULT_ADDR and VAULT_TOKEN are required")
        _validate_vault_address(
            address,
            allow_loopback_http=_environment_flag(
                self._environ,
                "SQLVERITY_VAULT_ALLOW_LOOPBACK_HTTP",
                default=False,
            ),
        )
        client = self._vault_client
        if client is None:
            try:
                httpx_module = import_module("httpx")
                client = httpx_module.Client(timeout=10.0)
            except (AttributeError, ImportError) as error:
                raise SecretResolutionError("httpx is required for Vault secrets") from error
            self._vault_client = client
        try:
            response = client.get(
                f"{address}/v1/{path}",
                headers={"X-Vault-Token": token},
            )
            response.raise_for_status()
            envelope = response.json()
            if not isinstance(envelope, Mapping):
                raise TypeError("Vault response is not an object")
            payload: object = envelope.get("data")
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
                payload = payload["data"]
            return _selected_secret_object(payload, parsed.fragment)
        except SecretResolutionError:
            raise
        except Exception as error:
            raise SecretResolutionError("Vault secret could not be resolved") from error

    def _aws_payload(self, parsed: Any) -> dict[str, Any]:
        secret_id = unquote(f"{parsed.netloc}{parsed.path}").strip("/")
        if not secret_id or parsed.query:
            raise SecretResolutionError("Invalid AWS Secrets Manager reference")
        client = self._aws_client
        if client is None:
            try:
                boto3_module = import_module("boto3")
                client = boto3_module.client(
                    "secretsmanager",
                    region_name=self._environ.get("AWS_REGION") or None,
                )
            except (AttributeError, ImportError) as error:
                raise SecretResolutionError(
                    "The secrets extra is required for AWS Secrets Manager"
                ) from error
            self._aws_client = client
        try:
            response = client.get_secret_value(SecretId=secret_id)
            secret_value = response.get("SecretString")
            if secret_value is None and response.get("SecretBinary") is not None:
                secret_value = b64decode(response["SecretBinary"]).decode("utf-8")
            payload = _json_secret_object(secret_value)
            return _selected_secret_object(payload, parsed.fragment)
        except SecretResolutionError:
            raise
        except Exception as error:
            raise SecretResolutionError(
                "AWS Secrets Manager secret could not be resolved"
            ) from error


def load_secret_resolver_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    vault_client: Any | None = None,
    aws_client: Any | None = None,
) -> SecretManagerResolver:
    environment = os.environ if environ is None else environ
    raw_backends = environment.get("SQLVERITY_SECRET_BACKENDS", "environment")
    items = tuple(item.strip().casefold() for item in raw_backends.split(","))
    if any(not item for item in items) or len(items) != len(set(items)):
        raise SecretResolutionError(
            "SQLVERITY_SECRET_BACKENDS must contain unique non-empty backend names"
        )
    try:
        return SecretManagerResolver(
            enabled_backends=frozenset(items),
            environ=environment,
            vault_client=vault_client,
            aws_client=aws_client,
        )
    except ValueError as error:
        raise SecretResolutionError(str(error)) from error


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError("value must be a non-empty string")
    return value.strip()


def _safe_host(value: object) -> str:
    host = _required_string(value)
    if any(character in host for character in ";{}"):
        raise ValueError("host contains connection-string delimiters")
    return host


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("port must be an integer")
    port = int(value)
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _positive_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("connect timeout must be an integer")
    timeout = int(value)
    if not 1 <= timeout <= 300:
        raise ValueError("connect timeout must be between 1 and 300 seconds")
    return timeout


def _postgresql_secret(payload: Mapping[str, Any]) -> PostgreSQLConnectionSecret:
    sslmode = payload.get("sslmode", "require")
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("sslmode is invalid")
    return PostgreSQLConnectionSecret(
        host=_safe_host(payload["host"]),
        database=_required_string(payload["database"]),
        username=_required_string(payload["username"]),
        password=_required_string(payload["password"]),
        port=_port(payload.get("port", 5432)),
        sslmode=cast(str, sslmode),
        connect_timeout_seconds=_positive_timeout(
            payload.get("connect_timeout_seconds", 10)
        ),
    )


def _mysql_secret(payload: Mapping[str, Any]) -> MySQLConnectionSecret:
    tls_required = payload.get("tls_required", True)
    ssl_ca = payload.get("ssl_ca")
    if not isinstance(tls_required, bool):
        raise TypeError("tls_required must be a boolean")
    if ssl_ca is not None and not isinstance(ssl_ca, str):
        raise TypeError("ssl_ca must be a string")
    return MySQLConnectionSecret(
        host=_safe_host(payload["host"]),
        database=_required_string(payload["database"]),
        username=_required_string(payload["username"]),
        password=_required_string(payload["password"]),
        port=_port(payload.get("port", 3306)),
        connect_timeout_seconds=_positive_timeout(
            payload.get("connect_timeout_seconds", 10)
        ),
        tls_required=tls_required,
        ssl_ca=ssl_ca,
    )


def _oracle_secret(payload: Mapping[str, Any]) -> OracleConnectionSecret:
    tls_required = payload.get("tls_required", True)
    wallet_location = payload.get("wallet_location")
    wallet_password = payload.get("wallet_password")
    if not isinstance(tls_required, bool):
        raise TypeError("tls_required must be a boolean")
    if wallet_location is not None and not isinstance(wallet_location, str):
        raise TypeError("wallet_location must be a string")
    if wallet_password is not None and not isinstance(wallet_password, str):
        raise TypeError("wallet_password must be a string")
    return OracleConnectionSecret(
        host=_safe_host(payload["host"]),
        service_name=_required_string(payload["service_name"]),
        username=_required_string(payload["username"]),
        password=_required_string(payload["password"]),
        port=_port(payload.get("port", 1521)),
        connect_timeout_seconds=_positive_timeout(
            payload.get("connect_timeout_seconds", 10)
        ),
        tls_required=tls_required,
        wallet_location=wallet_location,
        wallet_password=wallet_password,
    )


def _sqlserver_secret(payload: Mapping[str, Any]) -> SQLServerConnectionSecret:
    encrypt = payload.get("encrypt", True)
    trust_server_certificate = payload.get("trust_server_certificate", False)
    if not isinstance(encrypt, bool) or not isinstance(trust_server_certificate, bool):
        raise TypeError("encryption flags must be booleans")
    if not encrypt:
        raise ValueError("unencrypted SQL Server connection is not allowed")
    return SQLServerConnectionSecret(
        host=_safe_host(payload["host"]),
        database=_required_string(payload["database"]),
        username=_required_string(payload["username"]),
        password=_required_string(payload["password"]),
        port=_port(payload.get("port", 1433)),
        connect_timeout_seconds=_positive_timeout(
            payload.get("connect_timeout_seconds", 10)
        ),
        encrypt=encrypt,
        trust_server_certificate=trust_server_certificate,
    )


def _json_secret_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65_536:
        raise SecretResolutionError("Managed secret is missing or too large")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise SecretResolutionError("Managed secret is not valid JSON") from error
    if not isinstance(payload, dict):
        raise SecretResolutionError("Managed secret must be a JSON object")
    return cast(dict[str, Any], payload)


def _selected_secret_object(value: object, fragment: str) -> dict[str, Any]:
    selected = value
    if fragment:
        if not isinstance(selected, Mapping) or fragment not in selected:
            raise SecretResolutionError("Managed secret field does not exist")
        selected = selected[fragment]
    if not isinstance(selected, Mapping):
        raise SecretResolutionError("Managed secret payload must be an object")
    return {str(key): item for key, item in selected.items()}


def _validate_vault_address(address: str, *, allow_loopback_http: bool) -> None:
    parsed = urlparse(address)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SecretResolutionError("VAULT_ADDR is invalid")
    loopback = parsed.hostname.casefold() == "localhost" or parsed.hostname in {
        "127.0.0.1",
        "::1",
    }
    if parsed.scheme != "https" and not (loopback and allow_loopback_http):
        raise SecretResolutionError("Vault requires HTTPS")


def _environment_flag(
    environ: Mapping[str, str],
    name: str,
    *,
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
    raise SecretResolutionError(f"{name} must be a boolean")
