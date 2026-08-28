from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentConfigurationTests(unittest.TestCase):
    def test_compose_demo_database_is_isolated_and_read_only(self) -> None:
        root = Path(__file__).resolve().parents[2]
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        demo_line = next(
            line for line in compose.splitlines() if line.strip().startswith("SQLVERITY_DEMO_DB:")
        )
        fixture = (root / "fixtures" / "demo" / "postgresql.sql").read_text(
            encoding="utf-8"
        )
        initializer = (root / "fixtures" / "demo" / "00-create-demo.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('"database":"sqlverity_demo"', demo_line)
        self.assertIn('"username":"sqlverity_demo_reader"', demo_line)
        self.assertIn("${SQLVERITY_DEMO_DB_PASSWORD}", demo_line)
        self.assertNotIn("SQLVERITY_POSTGRES_PASSWORD", demo_line)
        self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT", initializer)
        self.assertIn("WHERE NOT EXISTS", initializer)
        self.assertIn("GRANT SELECT ON ALL TABLES", fixture)
        self.assertNotIn("GRANT INSERT", fixture)
        self.assertNotIn("GRANT CREATE", fixture)

    def test_container_load_gate_has_an_isolated_quota_budget(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("--env SQLVERITY_USER_REQUESTS_PER_WINDOW=1000", workflow)
        self.assertIn("--env SQLVERITY_USER_MAX_CONCURRENT=50", workflow)
        self.assertIn("--requests 200", workflow)
        self.assertIn("--concurrency 10", workflow)


if __name__ == "__main__":
    unittest.main()
