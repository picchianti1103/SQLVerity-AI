BEGIN;

CREATE TABLE authorized_query_definitions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    definition_version bigint NOT NULL CHECK (definition_version > 0),
    virtual_schema text NOT NULL,
    virtual_name text NOT NULL,
    description text NOT NULL,
    base_sql text NOT NULL,
    normalized_base_sql text NOT NULL,
    parameters jsonb NOT NULL DEFAULT '[]'::jsonb,
    allow_filtering boolean NOT NULL DEFAULT true,
    allow_aggregation boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, data_source_id, definition_version),
    UNIQUE (tenant_id, catalog_version_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id),
    CHECK (jsonb_typeof(parameters) = 'array')
);

CREATE INDEX authorized_query_definitions_lookup_idx
    ON authorized_query_definitions (tenant_id, data_source_id, definition_version DESC);

CREATE FUNCTION reject_authorized_query_definition_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'authorized_query_definitions are immutable';
END;
$$;

CREATE TRIGGER authorized_query_definitions_no_update_or_delete
BEFORE UPDATE OR DELETE ON authorized_query_definitions
FOR EACH ROW EXECUTE FUNCTION reject_authorized_query_definition_mutation();

ALTER TABLE query_requests
    ADD COLUMN parameter_names jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN parameter_value_hash text,
    ADD CONSTRAINT query_requests_parameter_names_array
        CHECK (jsonb_typeof(parameter_names) = 'array'),
    ADD CONSTRAINT query_requests_parameter_hash_format
        CHECK (parameter_value_hash IS NULL OR parameter_value_hash ~ '^[0-9a-f]{64}$');

COMMIT;
