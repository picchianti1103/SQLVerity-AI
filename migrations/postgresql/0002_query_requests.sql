BEGIN;

CREATE TABLE query_requests (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    sql_text text NOT NULL,
    normalized_sql text,
    referenced_tables jsonb NOT NULL,
    referenced_columns jsonb NOT NULL,
    validation_issue_codes jsonb NOT NULL,
    state text NOT NULL,
    approved_by text,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id),
    CHECK ((approved_by IS NULL) = (approved_at IS NULL))
);

CREATE INDEX query_requests_tenant_source_time_idx
    ON query_requests (tenant_id, data_source_id, created_at DESC);

COMMIT;
