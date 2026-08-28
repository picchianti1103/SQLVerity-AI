BEGIN;

ALTER TABLE provider_egress_policies
    ADD COLUMN acknowledgement_digest text,
    ADD COLUMN acknowledged_by text,
    ADD COLUMN acknowledged_at timestamptz,
    ADD CONSTRAINT provider_egress_policy_acknowledgement_atomic CHECK (
        (acknowledgement_digest IS NULL AND acknowledged_by IS NULL AND acknowledged_at IS NULL)
        OR
        (acknowledgement_digest IS NOT NULL AND acknowledged_by IS NOT NULL AND acknowledged_at IS NOT NULL)
    );

CREATE TABLE ai_transfer_receipts (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    actor_id text NOT NULL,
    provider_id text NOT NULL,
    model_id text NOT NULL,
    purpose text NOT NULL,
    privacy_mode text NOT NULL,
    provider_policy_id uuid,
    policy_scope text NOT NULL,
    provider_policy_version text,
    declared_classification text NOT NULL,
    detected_classification text NOT NULL,
    effective_classification text NOT NULL,
    maximum_allowed_classification text NOT NULL,
    detection_reason_codes jsonb NOT NULL,
    content_counts jsonb NOT NULL,
    preflight_digest text NOT NULL,
    confirmation_outcome text NOT NULL,
    provider_invoked boolean NOT NULL,
    decision_code text NOT NULL,
    llm_usage_event_id uuid REFERENCES llm_usage_events(id),
    query_request_id uuid,
    input_tokens integer,
    output_tokens integer,
    latency_ms integer,
    estimated_cost text,
    actual_cost text,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, query_request_id) REFERENCES query_requests(tenant_id, id),
    CHECK (jsonb_typeof(detection_reason_codes) = 'array'),
    CHECK (jsonb_typeof(content_counts) = 'array'),
    CHECK (length(preflight_digest) = 64),
    CHECK (input_tokens IS NULL OR input_tokens >= 0),
    CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CHECK (
        (provider_invoked AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND latency_ms IS NOT NULL)
        OR
        (NOT provider_invoked AND input_tokens IS NULL AND output_tokens IS NULL AND latency_ms IS NULL)
    )
);

CREATE INDEX ai_transfer_receipts_tenant_time_idx
    ON ai_transfer_receipts (tenant_id, created_at DESC);
CREATE INDEX ai_transfer_receipts_query_idx
    ON ai_transfer_receipts (tenant_id, query_request_id);

CREATE TABLE ai_preflight_confirmations (
    token_id text PRIMARY KEY,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE INDEX ai_preflight_confirmations_expiry_idx
    ON ai_preflight_confirmations (expires_at);

CREATE OR REPLACE FUNCTION reject_ai_transfer_receipt_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ai_transfer_receipts are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ai_transfer_receipts_no_update_or_delete
BEFORE UPDATE OR DELETE ON ai_transfer_receipts
FOR EACH ROW EXECUTE FUNCTION reject_ai_transfer_receipt_mutation();

COMMIT;
