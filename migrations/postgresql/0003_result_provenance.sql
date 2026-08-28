BEGIN;

ALTER TABLE query_requests
    ADD COLUMN business_concepts jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN assumptions jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN provider_id text,
    ADD COLUMN model_id text,
    ADD COLUMN llm_usage_event_id uuid,
    ADD COLUMN estimated_db_cost double precision,
    ADD COLUMN estimated_db_rows bigint,
    ADD COLUMN explained_at timestamptz,
    ADD CONSTRAINT query_requests_provider_model_pair
        CHECK ((provider_id IS NULL) = (model_id IS NULL)),
    ADD CONSTRAINT query_requests_estimated_db_cost_nonnegative
        CHECK (estimated_db_cost IS NULL OR estimated_db_cost >= 0),
    ADD CONSTRAINT query_requests_estimated_db_rows_nonnegative
        CHECK (estimated_db_rows IS NULL OR estimated_db_rows >= 0),
    ADD CONSTRAINT query_requests_llm_usage_fk
        FOREIGN KEY (llm_usage_event_id) REFERENCES llm_usage_events(id);

COMMIT;
