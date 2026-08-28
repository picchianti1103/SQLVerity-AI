from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packages.catalog.sqlverity_catalog.config import (
    CatalogConfigurationError,
    load_catalog_repository_from_environment,
)
from packages.connectors.sqlverity_connectors.connection import EnvironmentSecretResolver


class CatalogConfigurationTests(unittest.TestCase):
    def test_sqlite_configuration_uses_the_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            repository, backend = load_catalog_repository_from_environment(
                EnvironmentSecretResolver({}),
                {
                    "SQLVERITY_CATALOG_BACKEND": "sqlite",
                    "SQLVERITY_CATALOG_PATH": str(path),
                },
            )
            try:
                repository.initialize()
                self.assertTrue(repository.health_check())
            finally:
                repository.close()

        self.assertEqual("sqlite", backend)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(CatalogConfigurationError):
            load_catalog_repository_from_environment(
                EnvironmentSecretResolver({}),
                {"SQLVERITY_CATALOG_BACKEND": "unknown"},
            )


if __name__ == "__main__":
    unittest.main()
