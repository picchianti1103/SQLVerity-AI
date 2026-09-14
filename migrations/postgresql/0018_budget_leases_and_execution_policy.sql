BEGIN;

ALTER TABLE query_requests ADD COLUMN approved_cost_policy_revision INTEGER;
ALTER TABLE execution_cost_policies ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE llm_budget_accounts (
    tenant_id uuid NOT NULL REFERENCES tenants(id), currency TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tenant_id, currency)
);
CREATE TABLE llm_budget_reservations (
    id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
    provider_id TEXT NOT NULL, model_id TEXT NOT NULL, pricing_id uuid NOT NULL REFERENCES model_pricing(id),
    currency TEXT NOT NULL, amount TEXT NOT NULL, period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'in_flight', 'uncertain', 'settled', 'released')),
    actual_cost TEXT, usage_event_id uuid UNIQUE REFERENCES llm_usage_events(id) DEFERRABLE INITIALLY DEFERRED,
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL
);
CREATE INDEX llm_budget_reservations_scope_idx
    ON llm_budget_reservations(tenant_id, currency, period_start, state);
CREATE TABLE request_concurrency_leases (
    scope_key TEXT NOT NULL REFERENCES request_quota_windows(scope_key) ON DELETE CASCADE,
    lease_id TEXT NOT NULL, expires_at timestamptz NOT NULL,
    PRIMARY KEY (scope_key, lease_id)
);
CREATE INDEX request_concurrency_leases_expiry_idx
    ON request_concurrency_leases(scope_key, expires_at);

COMMIT;
