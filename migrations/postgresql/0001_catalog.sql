BEGIN;

CREATE TABLE tenants (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE data_sources (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name text NOT NULL,
    source_type text NOT NULL,
    dialect text NOT NULL,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    connection_secret_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name),
    UNIQUE (tenant_id, id)
);

CREATE TABLE catalog_versions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, data_source_id, version),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE TABLE schema_objects (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    schema_name text NOT NULL,
    object_name text NOT NULL,
    object_kind text NOT NULL,
    definition_sql text,
    UNIQUE (tenant_id, catalog_version_id, schema_name, object_name),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE column_definitions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    schema_object_id uuid NOT NULL,
    column_name text NOT NULL,
    physical_type text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    nullable boolean NOT NULL,
    classification text NOT NULL,
    default_expression text,
    is_primary_key boolean NOT NULL DEFAULT false,
    UNIQUE (tenant_id, schema_object_id, column_name),
    FOREIGN KEY (tenant_id, schema_object_id) REFERENCES schema_objects(tenant_id, id)
);

CREATE TABLE relationships (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    catalog_version_id uuid NOT NULL,
    source_object_id uuid NOT NULL,
    target_object_id uuid NOT NULL,
    relationship_name text NOT NULL,
    source_columns jsonb NOT NULL,
    target_columns jsonb NOT NULL,
    epistemic_status text NOT NULL CHECK (
        epistemic_status IN ('confirmed', 'imported', 'inferred', 'conflicting', 'unknown')
    ),
    source text NOT NULL,
    confidence numeric(4, 3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    UNIQUE (tenant_id, catalog_version_id, relationship_name),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_object_id) REFERENCES schema_objects(tenant_id, id),
    FOREIGN KEY (tenant_id, target_object_id) REFERENCES schema_objects(tenant_id, id)
);

CREATE TABLE semantic_definitions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    catalog_version_id uuid NOT NULL,
    object_ref text NOT NULL,
    description text NOT NULL,
    epistemic_status text NOT NULL CHECK (
        epistemic_status IN ('confirmed', 'imported', 'inferred', 'unknown')
    ),
    source text NOT NULL,
    confidence numeric(4, 3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    actor_id text,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE semantic_resolutions (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    object_ref text NOT NULL,
    description text NOT NULL,
    epistemic_status text NOT NULL CHECK (
        epistemic_status IN ('confirmed', 'imported', 'inferred', 'conflicting', 'unknown')
    ),
    confidence numeric(4, 3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    selected_definition_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, data_source_id, object_ref),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES semantic_definitions(tenant_id, id)
);

CREATE TABLE audit_events (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    event_type text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_usage_events (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    provider_id text NOT NULL,
    model_id text NOT NULL,
    purpose text NOT NULL,
    estimated_input_tokens bigint NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens bigint NOT NULL CHECK (estimated_output_tokens >= 0),
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    latency_ms bigint NOT NULL CHECK (latency_ms >= 0),
    estimated_cost text,
    actual_cost text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events are append-only';
END;
$$;

CREATE TRIGGER audit_events_no_update_or_delete
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();

CREATE OR REPLACE FUNCTION reject_semantic_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'semantic_definitions are immutable';
END;
$$;

CREATE TRIGGER semantic_definitions_no_update_or_delete
BEFORE UPDATE OR DELETE ON semantic_definitions
FOR EACH ROW EXECUTE FUNCTION reject_semantic_definition_mutation();

CREATE OR REPLACE FUNCTION reject_llm_usage_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'llm_usage_events are append-only';
END;
$$;

CREATE TRIGGER llm_usage_events_no_update_or_delete
BEFORE UPDATE OR DELETE ON llm_usage_events
FOR EACH ROW EXECUTE FUNCTION reject_llm_usage_event_mutation();

CREATE INDEX semantic_definitions_lookup_idx
    ON semantic_definitions (tenant_id, object_ref, created_at DESC);
CREATE INDEX relationships_source_idx
    ON relationships (tenant_id, source_object_id);
CREATE INDEX relationships_target_idx
    ON relationships (tenant_id, target_object_id);
CREATE INDEX audit_events_tenant_time_idx
    ON audit_events (tenant_id, created_at DESC);
CREATE INDEX llm_usage_events_tenant_time_idx
    ON llm_usage_events (tenant_id, created_at DESC);

COMMIT;
