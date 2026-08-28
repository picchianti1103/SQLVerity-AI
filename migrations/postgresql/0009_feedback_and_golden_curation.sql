BEGIN;

CREATE TABLE query_feedback_events (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    query_request_id uuid NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('accepted', 'rejected', 'corrected')),
    actor_id text NOT NULL,
    reason text,
    corrected_sql_example_id uuid,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, query_request_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, query_request_id) REFERENCES query_requests(tenant_id, id),
    FOREIGN KEY (tenant_id, corrected_sql_example_id)
        REFERENCES corrected_sql_examples(tenant_id, id),
    CHECK (
        (outcome = 'corrected' AND corrected_sql_example_id IS NOT NULL)
        OR (outcome <> 'corrected' AND corrected_sql_example_id IS NULL)
    )
);

CREATE TABLE golden_evaluation_candidates (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    catalog_version_id uuid NOT NULL,
    corrected_sql_example_id uuid NOT NULL,
    source_query_request_id uuid NOT NULL,
    question text NOT NULL,
    normalized_sql text NOT NULL,
    referenced_tables jsonb NOT NULL,
    referenced_columns jsonb NOT NULL,
    business_concepts jsonb NOT NULL DEFAULT '[]'::jsonb,
    assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
    content_classification text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, corrected_sql_example_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, corrected_sql_example_id)
        REFERENCES corrected_sql_examples(tenant_id, id),
    FOREIGN KEY (tenant_id, source_query_request_id)
        REFERENCES query_requests(tenant_id, id),
    CHECK (jsonb_typeof(referenced_tables) = 'array'),
    CHECK (jsonb_array_length(referenced_tables) > 0),
    CHECK (jsonb_typeof(referenced_columns) = 'array'),
    CHECK (jsonb_typeof(business_concepts) = 'array'),
    CHECK (jsonb_typeof(assumptions) = 'array')
);

CREATE TABLE golden_candidate_reviews (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    candidate_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor_id text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, candidate_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES golden_evaluation_candidates(tenant_id, id)
);

CREATE INDEX query_feedback_summary_idx
    ON query_feedback_events (tenant_id, data_source_id, outcome, created_at DESC);
CREATE INDEX golden_evaluation_candidates_lookup_idx
    ON golden_evaluation_candidates (tenant_id, data_source_id, created_at DESC);
CREATE INDEX golden_candidate_reviews_decision_idx
    ON golden_candidate_reviews (tenant_id, decision, created_at DESC);

CREATE FUNCTION reject_learning_governance_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER query_feedback_events_no_update_or_delete
BEFORE UPDATE OR DELETE ON query_feedback_events
FOR EACH ROW EXECUTE FUNCTION reject_learning_governance_mutation();
CREATE TRIGGER golden_evaluation_candidates_no_update_or_delete
BEFORE UPDATE OR DELETE ON golden_evaluation_candidates
FOR EACH ROW EXECUTE FUNCTION reject_learning_governance_mutation();
CREATE TRIGGER golden_candidate_reviews_no_update_or_delete
BEFORE UPDATE OR DELETE ON golden_candidate_reviews
FOR EACH ROW EXECUTE FUNCTION reject_learning_governance_mutation();

COMMIT;
