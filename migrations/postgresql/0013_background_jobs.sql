BEGIN;

CREATE TABLE background_jobs (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid,
    job_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_count integer NOT NULL CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    scheduled_at timestamptz NOT NULL,
    lease_expires_at timestamptz,
    worker_id text,
    result jsonb,
    last_error_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE INDEX background_jobs_claim_idx
    ON background_jobs (status, scheduled_at, created_at);
CREATE INDEX background_jobs_tenant_idx
    ON background_jobs (tenant_id, data_source_id, created_at DESC);
CREATE UNIQUE INDEX background_jobs_active_unique
    ON background_jobs (tenant_id, data_source_id, job_type)
    WHERE status IN ('queued', 'running');

COMMIT;
