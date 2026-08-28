from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from packages.connectors.sqlverity_connectors.connection import DatabaseSecretResolver

from .repository import PostgreSQLCatalogRepository, SQLiteCatalogRepository


class CatalogConfigurationError(RuntimeError):
    pass


def load_catalog_repository_from_environment(
    secret_resolver: DatabaseSecretResolver,
    environ: Mapping[str, str] | None = None,
    *,
    application_name: str = "sqlverity-catalog",
) -> tuple[SQLiteCatalogRepository, str]:
    environment = os.environ if environ is None else environ
    backend = environment.get("SQLVERITY_CATALOG_BACKEND", "sqlite").strip().casefold()
    if backend == "sqlite":
        path = Path(environment.get("SQLVERITY_CATALOG_PATH", "sqlverity_catalog.sqlite3"))
        return SQLiteCatalogRepository(path), "sqlite"
    if backend not in {"postgres", "postgresql"}:
        raise CatalogConfigurationError(
            "SQLVERITY_CATALOG_BACKEND must be either sqlite or postgresql"
        )
    secret_ref = environment.get("SQLVERITY_CATALOG_SECRET_REF", "").strip()
    if not secret_ref:
        raise CatalogConfigurationError(
            "SQLVERITY_CATALOG_SECRET_REF is required for the PostgreSQL catalog backend"
        )
    secret = secret_resolver.resolve_postgresql(secret_ref)
    min_pool_size = _bounded_environment_integer(
        environment,
        "SQLVERITY_CATALOG_POOL_MIN_SIZE",
        default=1,
        minimum=1,
        maximum=50,
    )
    max_pool_size = _bounded_environment_integer(
        environment,
        "SQLVERITY_CATALOG_POOL_MAX_SIZE",
        default=10,
        minimum=min_pool_size,
        maximum=100,
    )
    return (
        PostgreSQLCatalogRepository(
            secret.as_connect_kwargs(application_name=application_name),
            min_pool_size=min_pool_size,
            max_pool_size=max_pool_size,
        ),
        "postgresql",
    )


def _bounded_environment_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = environment.get(name, "").strip()
    try:
        value = default if not raw_value else int(raw_value)
    except ValueError as error:
        raise CatalogConfigurationError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise CatalogConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value
