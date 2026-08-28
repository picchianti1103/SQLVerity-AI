BEGIN;

CREATE TABLE request_quota_windows (
    scope_key text PRIMARY KEY,
    window_number bigint NOT NULL,
    request_count bigint NOT NULL CHECK (request_count >= 0),
    active_requests bigint NOT NULL CHECK (active_requests >= 0),
    updated_at timestamptz NOT NULL
);

CREATE INDEX request_quota_windows_updated_idx
    ON request_quota_windows (updated_at);

COMMIT;
