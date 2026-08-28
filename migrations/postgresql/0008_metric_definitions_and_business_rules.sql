BEGIN;

CREATE TABLE analytic_semantic_definitions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('metric', 'business_rule')),
    asset_key text NOT NULL,
    payload jsonb NOT NULL,
    content_classification text NOT NULL,
    epistemic_status text NOT NULL CHECK (epistemic_status <> 'conflicting'),
    source text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    actor_id text,
    reason text,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    CHECK (jsonb_typeof(payload) = 'object')
);

CREATE TABLE analytic_semantic_resolutions (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('metric', 'business_rule')),
    asset_key text NOT NULL,
    payload jsonb NOT NULL,
    content_classification text NOT NULL,
    epistemic_status text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    selected_definition_id uuid,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, data_source_id, asset_kind, asset_key),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES analytic_semantic_definitions(tenant_id, id),
    CHECK (jsonb_typeof(payload) = 'object')
);

CREATE INDEX analytic_semantic_definitions_history_idx
    ON analytic_semantic_definitions
        (tenant_id, data_source_id, asset_kind, asset_key, created_at DESC);
CREATE INDEX analytic_semantic_resolutions_review_idx
    ON analytic_semantic_resolutions
        (tenant_id, data_source_id, asset_kind, epistemic_status, updated_at DESC);

CREATE FUNCTION reject_analytic_semantic_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'analytic_semantic_definitions are immutable';
END;
$$;

CREATE TRIGGER analytic_semantic_definitions_no_update_or_delete
BEFORE UPDATE OR DELETE ON analytic_semantic_definitions
FOR EACH ROW EXECUTE FUNCTION reject_analytic_semantic_definition_mutation();

ALTER TABLE query_requests
    ADD COLUMN metrics jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN business_rules jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT query_requests_metrics_array
        CHECK (jsonb_typeof(metrics) = 'array'),
    ADD CONSTRAINT query_requests_business_rules_array
        CHECK (jsonb_typeof(business_rules) = 'array');

COMMIT;
