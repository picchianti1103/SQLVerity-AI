from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from playwright.sync_api import Browser, Page, Route, expect, sync_playwright

from apps.api.main import app
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import DataSourceType, ObjectKind, PlatformRole
from packages.security.sqlverity_security import AuthenticationService
from tests.unit.api_security import TEST_BOOTSTRAP_KEY, api_test_environment


@unittest.skipUnless(
    os.environ.get("SQLVERITY_RUN_BROWSER_TESTS") == "true", "Browser tests opt in"
)
class ConsoleBrowserTests(unittest.TestCase):
    browser: Browser
    base_url: str
    fixture: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        environment = api_test_environment(Path(temporary.name) / "browser.sqlite3")
        environment.update(
            SQLVERITY_CATALOG_BACKEND="sqlite", SQLVERITY_USER_REQUESTS_PER_WINDOW="10000"
        )
        patched = patch.dict(os.environ, environment)
        patched.start()
        cls.addClassCleanup(patched.stop)
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        cls.base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        def stop_server() -> None:
            server.should_exit = True
            thread.join(timeout=10)

        cls.addClassCleanup(stop_server)
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(f"{cls.base_url}/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (URLError, OSError):
                if time.monotonic() > deadline:
                    raise RuntimeError("Synthetic browser server did not start") from None
                time.sleep(0.05)
        repository = cast(SQLiteCatalogRepository, app.state.catalog)
        security = cast(AuthenticationService, app.state.security)
        cls.fixture = {}
        for label in ("Alpha", "Beta"):
            tenant = repository.create_tenant(label)
            source = repository.create_data_source(
                tenant_id=tenant.id,
                name=f"{label} source",
                source_type=DataSourceType.MANUAL_SCHEMA,
                dialect="postgresql",
            )
            version = repository.create_catalog_version(tenant.id, source.id)
            table = repository.create_schema_object(
                tenant_id=tenant.id,
                data_source_id=source.id,
                catalog_version_id=version.id,
                schema_name="public",
                name=f"{label.lower()}_orders",
                kind=ObjectKind.TABLE,
            )
            repository.create_column(
                tenant_id=tenant.id,
                schema_object_id=table.id,
                name="id",
                physical_type="integer",
                ordinal=1,
                nullable=False,
            )
            cls.fixture[label] = dict(tenant=tenant.id, source=source.id)
        alpha = cls.fixture["Alpha"]
        sibling = repository.create_data_source(
            tenant_id=alpha["tenant"],
            name="Hidden sibling",
            source_type=DataSourceType.MANUAL_SCHEMA,
            dialect="postgresql",
        )
        cls.fixture["sibling"] = sibling.id
        for role in (PlatformRole.ANALYST, PlatformRole.VIEWER):
            access = security.provision_principal(
                tenant_id=alpha["tenant"],
                subject=f"{role.value}@example.test",
                display_name=role.value,
                role=role,
                credential_label="browser synthetic",
                created_by="bootstrap-admin",
                data_source_ids=(alpha["source"],),
            )
            cls.fixture[role.value] = access.api_key
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        channel = os.environ.get("SQLVERITY_BROWSER_CHANNEL") or None
        cls.browser = playwright.chromium.launch(headless=True, channel=channel)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self) -> None:
        context = self.browser.new_context(base_url=self.base_url)
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self) -> None:
        self.assertEqual([], self.errors)

    def login(self, token: str) -> Page:
        self.page.goto("/ui")
        self.page.locator("#api-token").fill(token)
        self.page.locator("#connection-form button[type=submit]").click()
        expect(self.page.locator("#tenant-select option")).not_to_have_count(1)
        return self.page

    def test_scoped_analyst_can_discover_and_open_only_the_granted_source(self) -> None:
        page = self.login(self.fixture["analyst"])
        expect(page.locator("#tenant-select")).to_have_value(self.fixture["Alpha"]["tenant"])
        page.locator('[data-panel="sources"]').click()
        expect(page.locator("#source-list .source-item")).to_have_count(1)
        expect(page.locator("#source-list")).to_contain_text("Alpha source")
        expect(page.locator("#source-form")).to_be_hidden()
        page.locator("#source-list .source-item").click()
        expect(page.locator('[data-panel="query"]')).to_be_enabled()
        page.locator('[data-panel="schema"]').click()
        page.locator("#refresh-schema").click()
        expect(page.locator("#object-list")).to_contain_text("public.alpha_orders")
        response = page.request.get(
            f"/v1/tenants/{self.fixture['Alpha']['tenant']}/data-sources/{self.fixture['sibling']}",
            headers={"Authorization": f"Bearer {self.fixture['analyst']}"},
        )
        self.assertEqual(403, response.status)

    def test_scoped_viewer_can_inspect_schema_but_cannot_enter_query_or_manage_access(self) -> None:
        page = self.login(self.fixture["viewer"])
        page.locator('[data-panel="sources"]').click()
        page.locator("#source-list .source-item").click()
        expect(page.locator('[data-panel="query"]')).to_be_disabled()
        page.locator('[data-panel="admin"]').click()
        expect(page.locator("#federated-principal-form")).to_be_hidden()
        expect(page.locator("#test-connection")).to_be_disabled()

    def test_tenant_change_while_schema_is_pending_keeps_the_new_context(self) -> None:
        page = self.login(TEST_BOOTSTRAP_KEY)
        page.locator("#tenant-select").select_option(self.fixture["Alpha"]["tenant"])
        page.locator('[data-panel="sources"]').click()
        page.locator(f'[data-source-id="{self.fixture["Alpha"]["source"]}"]').click()
        pending: list[Route] = []
        path = (
            f"/v1/tenants/{self.fixture['Alpha']['tenant']}/data-sources/"
            f"{self.fixture['Alpha']['source']}/schema"
        )
        page.route(f"**{path}", lambda route: pending.append(route))
        page.locator('[data-panel="schema"]').click()
        page.locator("#refresh-schema").click()
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        self.assertEqual(1, len(pending))
        page.locator('[data-panel="system"]').click()
        page.locator("#tenant-select").select_option(self.fixture["Beta"]["tenant"])
        page.locator('[data-panel="sources"]').click()
        page.locator(f'[data-source-id="{self.fixture["Beta"]["source"]}"]').click()
        page.locator('[data-panel="schema"]').click()
        page.locator("#refresh-schema").click()
        expect(page.locator("#object-list")).to_contain_text("public.beta_orders")
        pending[0].fulfill(status=500, json={"detail": "Late Alpha failure"})
        expect(page.locator("#object-list")).to_contain_text("public.beta_orders")
        expect(page.locator("#flash")).not_to_contain_text("Late Alpha failure")
