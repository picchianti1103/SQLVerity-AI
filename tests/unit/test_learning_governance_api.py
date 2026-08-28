from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.catalog.sqlverity_catalog.repository import SQLiteCatalogRepository
from packages.domain.sqlverity_domain.models import QueryRequest, QueryRequestState
from tests.unit.api_security import TEST_AUTH_HEADERS, api_test_environment


class LearningGovernanceAPITests(unittest.TestCase):
    def test_feedback_candidate_review_and_export_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            catalog_path = Path(temporary_directory) / "catalog.sqlite3"
            with patch.dict(os.environ, api_test_environment(catalog_path)):
                with TestClient(app, headers=TEST_AUTH_HEADERS) as client:
                    tenant = client.post("/v1/tenants", json={"name": "Acme"}).json()
                    source = client.post(
                        f"/v1/tenants/{tenant['id']}/data-sources",
                        json={
                            "name": "Governed learning",
                            "source_type": "manual_schema",
                            "dialect": "postgresql",
                        },
                    ).json()
                    base_path = (
                        f"/v1/tenants/{tenant['id']}/data-sources/{source['id']}"
                    )
                    imported = client.post(
                        f"{base_path}/imports/manual",
                        json={
                            "objects": [
                                {
                                    "schema_name": "public",
                                    "name": "orders",
                                    "kind": "table",
                                    "columns": [
                                        {
                                            "name": "id",
                                            "physical_type": "bigint",
                                            "ordinal": 1,
                                            "nullable": False,
                                        },
                                        {
                                            "name": "total_amount",
                                            "physical_type": "numeric",
                                            "ordinal": 2,
                                            "nullable": False,
                                        },
                                    ],
                                }
                            ]
                        },
                    )
                    self.assertEqual(imported.status_code, 201, imported.text)
                    repository = cast(SQLiteCatalogRepository, app.state.catalog)
                    version = repository.get_latest_catalog_version(
                        tenant["id"],
                        source["id"],
                    )
                    assert version is not None
                    query_request = repository.create_query_request(
                        QueryRequest(
                            tenant_id=tenant["id"],
                            data_source_id=source["id"],
                            catalog_version_id=version.id,
                            sql_text="SELECT id FROM public.orders",
                            normalized_sql="SELECT id FROM public.orders LIMIT 500",
                            referenced_tables=("public.orders",),
                            referenced_columns=("public.orders.id",),
                            validation_issue_codes=(),
                            state=QueryRequestState.READY_FOR_PREVIEW,
                        )
                    )
                    correction = client.post(
                        f"{base_path}/learning/sql-examples",
                        json={
                            "question": "Mostra il valore totale degli ordini",
                            "corrected_sql": (
                                "SELECT id, total_amount FROM public.orders"
                            ),
                            "content_classification": "internal",
                            "business_concepts": ["gross_order_value"],
                            "source_query_request_id": query_request.id,
                        },
                    )
                    self.assertEqual(correction.status_code, 201, correction.text)
                    example_id = correction.json()["example"]["id"]

                    candidate_path = f"{base_path}/learning/golden-candidates"
                    premature = client.post(
                        candidate_path,
                        json={"corrected_sql_example_id": example_id},
                    )
                    self.assertEqual(premature.status_code, 422, premature.text)

                    feedback_path = (
                        f"{base_path}/query-requests/{query_request.id}/feedback"
                    )
                    feedback = client.post(
                        feedback_path,
                        json={
                            "outcome": "corrected",
                            "reason": "Corrected after business review",
                            "corrected_sql_example_id": example_id,
                        },
                    )
                    self.assertEqual(feedback.status_code, 201, feedback.text)
                    self.assertEqual(feedback.json()["outcome"], "corrected")
                    duplicate = client.post(
                        feedback_path,
                        json={
                            "outcome": "accepted",
                        },
                    )
                    self.assertEqual(duplicate.status_code, 409, duplicate.text)

                    summary = client.get(f"{base_path}/feedback/summary")
                    self.assertEqual(summary.status_code, 200, summary.text)
                    self.assertEqual(
                        summary.json(),
                        {
                            "total_count": 1,
                            "accepted_count": 0,
                            "rejected_count": 0,
                            "corrected_count": 1,
                            "acceptance_rate": 0.0,
                            "correction_rate": 1.0,
                        },
                    )

                    proposed = client.post(
                        candidate_path,
                        json={"corrected_sql_example_id": example_id},
                    )
                    self.assertEqual(proposed.status_code, 201, proposed.text)
                    proposed_body = proposed.json()
                    self.assertEqual(proposed_body["status"], "proposed")
                    candidate_id = proposed_body["candidate"]["id"]

                    listed = client.get(
                        candidate_path,
                        params={"candidate_status": "proposed"},
                    )
                    self.assertEqual(listed.status_code, 200, listed.text)
                    self.assertEqual(len(listed.json()), 1)
                    invalid_review = client.post(
                        f"{candidate_path}/{candidate_id}/reviews",
                        json={
                            "decision": "proposed",
                        },
                    )
                    self.assertEqual(invalid_review.status_code, 422, invalid_review.text)
                    approved = client.post(
                        f"{candidate_path}/{candidate_id}/reviews",
                        json={
                            "decision": "approved",
                            "reason": "Golden case approved",
                        },
                    )
                    self.assertEqual(approved.status_code, 201, approved.text)
                    self.assertEqual(approved.json()["status"], "approved")
                    second_review = client.post(
                        f"{candidate_path}/{candidate_id}/reviews",
                        json={
                            "decision": "rejected",
                        },
                    )
                    self.assertEqual(second_review.status_code, 409, second_review.text)

                    exported = client.get(f"{candidate_path}/export")
                    self.assertEqual(exported.status_code, 200, exported.text)
                    export_body = exported.json()
                    self.assertEqual(export_body["format_version"], 1)
                    self.assertEqual(len(export_body["candidates"]), 1)
                    self.assertEqual(
                        export_body["candidates"][0]["candidate_id"],
                        candidate_id,
                    )
                    self.assertEqual(
                        export_body["candidates"][0]["normalized_sql"],
                        "SELECT id, total_amount FROM public.orders LIMIT 500",
                    )


if __name__ == "__main__":
    unittest.main()
