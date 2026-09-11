from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.cost_engine.sqlverity_cost_engine import FinOpsService
from packages.domain.sqlverity_domain.models import ModelPricing, TenantBudget
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class BudgetReservationAPITests(unittest.TestCase):
    def test_reconciliation_is_scoped_authorized_and_audited(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                api_test_environment(Path(directory) / "catalog.sqlite3"),
            ),
            TestClient(app, headers=TEST_AUTH_HEADERS) as client,
        ):
            repository = cast(SQLiteCatalogRepository, app.state.catalog)
            tenant = repository.create_tenant("FinOps tenant")
            other = repository.create_tenant("Other tenant")
            valid_from = datetime.now(UTC) - timedelta(days=1)
            repository.create_model_pricing(
                ModelPricing(
                    tenant_id=tenant.id,
                    provider_id="fake",
                    model_id="fake",
                    currency="USD",
                    input_price_per_unit=Decimal("1"),
                    output_price_per_unit=Decimal("1"),
                    token_unit=1,
                    source_version="synthetic",
                    valid_from=valid_from,
                )
            )
            repository.create_tenant_budget(
                TenantBudget(
                    tenant_id=tenant.id,
                    currency="USD",
                    amount=Decimal("10"),
                    valid_from=valid_from,
                )
            )
            finops = FinOpsService(repository)
            estimate = finops.estimate(
                tenant_id=tenant.id,
                provider_id="fake",
                model_id="fake",
                input_tokens=2,
                output_tokens=3,
            )
            assert estimate is not None
            reservation = finops.reserve(tenant.id, "fake", "fake", estimate)
            finops.uncertain(reservation)
            base = f"/v1/tenants/{tenant.id}/finops"
            path = f"{base}/reservations/{reservation.id}/reconcile"
            listed = client.get(f"{base}/reservations")
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual(reservation.id, listed.json()[0]["id"])
            summary = client.get(f"{base}/summary").json()
            self.assertEqual(Decimal("5"), Decimal(summary["reserved_cost"]))
            self.assertEqual(Decimal("5"), Decimal(summary["uncertain_cost"]))
            self.assertEqual(Decimal("5"), Decimal(summary["remaining_amount"]))

            viewer = client.post(
                f"/v1/tenants/{tenant.id}/security/principals",
                json={
                    "subject": "viewer@example.test",
                    "display_name": "Viewer",
                    "role": "viewer",
                    "credential_label": "test-key",
                    "data_source_ids": [],
                },
            )
            self.assertEqual(201, viewer.status_code, viewer.text)
            headers = {"Authorization": f"Bearer {viewer.json()['api_key']}"}
            self.assertEqual(
                403,
                client.post(
                    path,
                    headers=headers,
                    json={
                        "actual_cost": "0",
                        "reason": "Not permitted",
                    },
                ).status_code,
            )
            self.assertEqual(
                404,
                client.post(
                    f"/v1/tenants/{other.id}/finops/reservations/{reservation.id}/reconcile",
                    json={"actual_cost": "0", "reason": "Wrong tenant"},
                ).status_code,
            )
            self.assertEqual(
                422,
                client.post(
                    path,
                    json={
                        "actual_cost": "-1",
                        "reason": "Invalid credit",
                    },
                ).status_code,
            )
            self.assertEqual(
                422,
                client.post(
                    path,
                    json={
                        "actual_cost": "0",
                        "reason": "   ",
                    },
                ).status_code,
            )
            settled = client.post(
                path,
                json={
                    "actual_cost": "3",
                    "reason": "Provider invoice confirmed three units",
                },
            )
            self.assertEqual(200, settled.status_code, settled.text)
            self.assertEqual("settled", settled.json()["state"])
            self.assertEqual(
                409,
                client.post(
                    path,
                    json={
                        "actual_cost": "0",
                        "reason": "Already settled",
                    },
                ).status_code,
            )
            summary = client.get(f"{base}/summary").json()
            self.assertEqual(Decimal("3"), Decimal(summary["reconciled_cost"]))
            self.assertEqual(Decimal("7"), Decimal(summary["remaining_amount"]))
            audits = [
                event
                for event in repository.audit_events(tenant.id)
                if event.event_type == "finops.reservation_reconciled"
            ]
            self.assertEqual(1, len(audits))
            self.assertTrue(audits[0].details["actor_id"])
