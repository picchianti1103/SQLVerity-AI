from __future__ import annotations

from pathlib import Path

TEST_BOOTSTRAP_KEY = "test-bootstrap-api-key-with-at-least-32-chars"
TEST_AUTH_HEADERS = {"Authorization": f"Bearer {TEST_BOOTSTRAP_KEY}"}


def api_test_environment(catalog_path: Path) -> dict[str, str]:
    return {
        "SQLVERITY_CATALOG_PATH": str(catalog_path),
        "SQLVERITY_BOOTSTRAP_API_KEY": TEST_BOOTSTRAP_KEY,
        "SQLVERITY_LLM_PROVIDER": "",
        "SQLVERITY_LLM_PROVIDERS": "",
        "SQLVERITY_REQUIRE_PROVIDER_POLICY": "false",
        "SQLVERITY_SECRET_BACKENDS": "environment",
        "SQLVERITY_BACKGROUND_WORKER_ENABLED": "false",
        "SQLVERITY_OIDC_ISSUER": "",
        "SQLVERITY_OIDC_CLIENT_ID": "",
        "SQLVERITY_OIDC_AUTHORIZATION_ENDPOINT": "",
        "SQLVERITY_OIDC_TOKEN_ENDPOINT": "",
        "SQLVERITY_OIDC_REDIRECT_URI": "",
        "SQLVERITY_BROWSER_SESSION_SECRET": "",
    }
