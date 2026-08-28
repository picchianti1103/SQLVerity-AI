BEGIN;

CREATE TABLE corrected_sql_examples (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    question text NOT NULL,
    normalized_question text NOT NULL,
    content_classification text NOT NULL CHECK (
        content_classification IN (
            'public', 'internal', 'confidential', 'pii', 'highly_sensitive'
        )
    ),
    sql_text text NOT NULL,
    normalized_sql text NOT NULL,
    referenced_tables jsonb NOT NULL,
    referenced_columns jsonb NOT NULL,
    business_concepts jsonb NOT NULL DEFAULT '[]'::jsonb,
    assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
    actor_id text NOT NULL,
    reason text,
    source_query_request_id uuid,
    supersedes_example_id uuid,
    revision bigint NOT NULL CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_query_request_id)
        REFERENCES query_requests(tenant_id, id),
    FOREIGN KEY (tenant_id, supersedes_example_id)
        REFERENCES corrected_sql_examples(tenant_id, id),
    CHECK (jsonb_typeof(referenced_tables) = 'array'),
    CHECK (jsonb_typeof(referenced_columns) = 'array'),
    CHECK (jsonb_typeof(business_concepts) = 'array'),
    CHECK (jsonb_typeof(assumptions) = 'array'),
    CHECK (
        (revision = 1 AND supersedes_example_id IS NULL)
        OR (revision > 1 AND supersedes_example_id IS NOT NULL)
    )
);

CREATE INDEX corrected_sql_examples_lookup_idx
    ON corrected_sql_examples
        (tenant_id, data_source_id, normalized_question, created_at DESC);

CREATE UNIQUE INDEX corrected_sql_examples_root_unique
    ON corrected_sql_examples (tenant_id, data_source_id, normalized_question)
    WHERE supersedes_example_id IS NULL;

CREATE UNIQUE INDEX corrected_sql_examples_successor_unique
    ON corrected_sql_examples (tenant_id, supersedes_example_id)
    WHERE supersedes_example_id IS NOT NULL;

CREATE FUNCTION reject_corrected_sql_example_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'corrected_sql_examples are immutable';
END;
$$;

CREATE TRIGGER corrected_sql_examples_no_update_or_delete
BEFORE UPDATE OR DELETE ON corrected_sql_examples
FOR EACH ROW EXECUTE FUNCTION reject_corrected_sql_example_mutation();

COMMIT;
