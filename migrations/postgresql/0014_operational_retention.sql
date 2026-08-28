BEGIN;

CREATE TABLE operational_retention_runs (
    id uuid PRIMARY KEY,
    cutoff timestamptz NOT NULL,
    background_jobs_deleted integer NOT NULL CHECK (background_jobs_deleted >= 0),
    quota_windows_deleted integer NOT NULL CHECK (quota_windows_deleted >= 0),
    actor_id text NOT NULL,
    completed_at timestamptz NOT NULL
);

CREATE FUNCTION reject_operational_retention_run_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'operational_retention_runs are append-only';
END;
$$;

CREATE TRIGGER operational_retention_runs_no_update_or_delete
BEFORE UPDATE OR DELETE ON operational_retention_runs
FOR EACH ROW EXECUTE FUNCTION reject_operational_retention_run_mutation();

COMMIT;
