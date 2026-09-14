BEGIN;

ALTER TABLE query_requests
    ADD COLUMN explain_revision integer NOT NULL DEFAULT 0,
    ADD COLUMN explained_sql_hash text,
    ADD COLUMN approved_explain_revision integer,
    ADD COLUMN approved_sql_hash text,
    ADD COLUMN approved_parameter_value_hash text;

CREATE TABLE query_explain_revisions (
    tenant_id uuid NOT NULL,
    request_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    sql_hash text NOT NULL,
    parameter_value_hash text,
    parameter_names jsonb NOT NULL,
    estimated_db_cost double precision,
    estimated_db_rows bigint,
    elapsed_ms integer NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, request_id, revision),
    FOREIGN KEY (tenant_id, request_id) REFERENCES query_requests(tenant_id, id)
);

CREATE FUNCTION reject_query_explain_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'query_explain_revisions are immutable';
END;
$$;

CREATE TRIGGER query_explain_revisions_no_update_or_delete
BEFORE UPDATE OR DELETE ON query_explain_revisions
FOR EACH ROW EXECUTE FUNCTION reject_query_explain_revision_mutation();

COMMIT;
