BEGIN;

CREATE TABLE provider_egress_policies (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid,
    provider_id text NOT NULL,
    allowed boolean NOT NULL,
    maximum_classification text NOT NULL CHECK (
        maximum_classification IN (
            'public', 'internal', 'confidential', 'pii', 'highly_sensitive'
        )
    ),
    allowed_purposes jsonb NOT NULL,
    data_residency text NOT NULL,
    retention_mode text NOT NULL CHECK (
        retention_mode IN ('zero', 'temporary', 'provider_default', 'local_runtime')
    ),
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    CHECK (jsonb_typeof(allowed_purposes) = 'array'),
    CHECK (jsonb_array_length(allowed_purposes) > 0)
);

CREATE UNIQUE INDEX provider_egress_policies_tenant_unique
    ON provider_egress_policies (tenant_id, provider_id)
    WHERE data_source_id IS NULL;
CREATE UNIQUE INDEX provider_egress_policies_source_unique
    ON provider_egress_policies (tenant_id, data_source_id, provider_id)
    WHERE data_source_id IS NOT NULL;
CREATE INDEX provider_egress_policies_lookup_idx
    ON provider_egress_policies (tenant_id, data_source_id, provider_id);

COMMIT;
