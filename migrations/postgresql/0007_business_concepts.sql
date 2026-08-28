BEGIN;

CREATE TABLE business_concept_definitions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    concept_key text NOT NULL,
    concept_name text NOT NULL,
    description text NOT NULL,
    synonyms jsonb NOT NULL DEFAULT '[]'::jsonb,
    object_refs jsonb NOT NULL,
    content_classification text NOT NULL,
    epistemic_status text NOT NULL,
    source text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    actor_id text,
    reason text,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    CHECK (jsonb_typeof(synonyms) = 'array'),
    CHECK (jsonb_typeof(object_refs) = 'array'),
    CHECK (jsonb_array_length(object_refs) > 0),
    CHECK (epistemic_status <> 'conflicting')
);

CREATE TABLE business_concept_resolutions (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    concept_key text NOT NULL,
    concept_name text NOT NULL,
    description text NOT NULL,
    synonyms jsonb NOT NULL DEFAULT '[]'::jsonb,
    object_refs jsonb NOT NULL,
    content_classification text NOT NULL,
    epistemic_status text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    selected_definition_id uuid,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, data_source_id, concept_key),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES business_concept_definitions(tenant_id, id),
    CHECK (jsonb_typeof(synonyms) = 'array'),
    CHECK (jsonb_typeof(object_refs) = 'array'),
    CHECK (jsonb_array_length(object_refs) > 0)
);

CREATE INDEX business_concept_definitions_history_idx
    ON business_concept_definitions
        (tenant_id, data_source_id, concept_key, created_at DESC);
CREATE INDEX business_concept_resolutions_review_idx
    ON business_concept_resolutions
        (tenant_id, data_source_id, epistemic_status, updated_at DESC);

CREATE FUNCTION reject_business_concept_definition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'business_concept_definitions are immutable';
END;
$$;

CREATE TRIGGER business_concept_definitions_no_update_or_delete
BEFORE UPDATE OR DELETE ON business_concept_definitions
FOR EACH ROW EXECUTE FUNCTION reject_business_concept_definition_mutation();

COMMIT;
