PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    dialect TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    connection_secret_ref TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, name),
    UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS catalog_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, data_source_id, version),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS schema_objects (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    object_name TEXT NOT NULL,
    object_kind TEXT NOT NULL,
    definition_sql TEXT,
    UNIQUE (tenant_id, catalog_version_id, schema_name, object_name),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS column_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    schema_object_id TEXT NOT NULL,
    column_name TEXT NOT NULL,
    physical_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    nullable INTEGER NOT NULL CHECK (nullable IN (0, 1)),
    classification TEXT NOT NULL,
    default_expression TEXT,
    is_primary_key INTEGER NOT NULL DEFAULT 0 CHECK (is_primary_key IN (0, 1)),
    UNIQUE (tenant_id, schema_object_id, column_name),
    FOREIGN KEY (tenant_id, schema_object_id) REFERENCES schema_objects(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    catalog_version_id TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    relationship_name TEXT NOT NULL,
    source_columns_json TEXT NOT NULL,
    target_columns_json TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    UNIQUE (tenant_id, catalog_version_id, relationship_name),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_object_id) REFERENCES schema_objects(tenant_id, id),
    FOREIGN KEY (tenant_id, target_object_id) REFERENCES schema_objects(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS semantic_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    catalog_version_id TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    description TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    actor_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS semantic_resolutions (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    description TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    selected_definition_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, data_source_id, object_ref),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES semantic_definitions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS business_concept_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    concept_key TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    description TEXT NOT NULL,
    synonyms_json TEXT NOT NULL,
    object_refs_json TEXT NOT NULL,
    content_classification TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    actor_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS business_concept_resolutions (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    concept_key TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    description TEXT NOT NULL,
    synonyms_json TEXT NOT NULL,
    object_refs_json TEXT NOT NULL,
    content_classification TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    selected_definition_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, data_source_id, concept_key),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES business_concept_definitions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS analytic_semantic_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('metric', 'business_rule')),
    asset_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_classification TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    actor_id TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS analytic_semantic_resolutions (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    asset_kind TEXT NOT NULL CHECK (asset_kind IN ('metric', 'business_rule')),
    asset_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_classification TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    selected_definition_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, data_source_id, asset_kind, asset_key),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, selected_definition_id)
        REFERENCES analytic_semantic_definitions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens INTEGER NOT NULL CHECK (estimated_output_tokens >= 0),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    estimated_cost TEXT,
    actual_cost TEXT,
    currency TEXT,
    pricing_id TEXT REFERENCES model_pricing(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_pricing (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    currency TEXT NOT NULL,
    token_unit INTEGER NOT NULL CHECK (token_unit > 0),
    input_price_per_unit TEXT NOT NULL,
    cached_input_price_per_unit TEXT,
    output_price_per_unit TEXT NOT NULL,
    batch_discount TEXT NOT NULL,
    notes TEXT,
    source_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS tenant_budgets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    currency TEXT NOT NULL,
    amount TEXT NOT NULL,
    period TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS execution_cost_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    max_total_cost REAL,
    max_estimated_rows INTEGER,
    require_explain INTEGER NOT NULL CHECK (require_explain IN (0, 1)),
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, data_source_id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS provider_egress_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT,
    provider_id TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    maximum_classification TEXT NOT NULL,
    allowed_purposes_json TEXT NOT NULL,
    data_residency TEXT NOT NULL,
    retention_mode TEXT NOT NULL,
    acknowledgement_digest TEXT,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    CHECK (
        (acknowledgement_digest IS NULL AND acknowledged_by IS NULL AND acknowledged_at IS NULL)
        OR
        (acknowledgement_digest IS NOT NULL AND acknowledged_by IS NOT NULL AND acknowledged_at IS NOT NULL)
    ),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS provider_egress_policies_tenant_unique
    ON provider_egress_policies (tenant_id, provider_id)
    WHERE data_source_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS provider_egress_policies_source_unique
    ON provider_egress_policies (tenant_id, data_source_id, provider_id)
    WHERE data_source_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ai_transfer_receipts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    privacy_mode TEXT NOT NULL,
    provider_policy_id TEXT,
    policy_scope TEXT NOT NULL,
    provider_policy_version TEXT,
    declared_classification TEXT NOT NULL,
    detected_classification TEXT NOT NULL,
    effective_classification TEXT NOT NULL,
    maximum_allowed_classification TEXT NOT NULL,
    detection_reason_codes_json TEXT NOT NULL,
    content_counts_json TEXT NOT NULL,
    preflight_digest TEXT NOT NULL,
    confirmation_outcome TEXT NOT NULL,
    provider_invoked INTEGER NOT NULL CHECK (provider_invoked IN (0, 1)),
    decision_code TEXT NOT NULL,
    llm_usage_event_id TEXT,
    query_request_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    estimated_cost TEXT,
    actual_cost TEXT,
    created_at TEXT NOT NULL,
    CHECK (input_tokens IS NULL OR input_tokens >= 0),
    CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CHECK (
        (provider_invoked = 1 AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND latency_ms IS NOT NULL)
        OR
        (provider_invoked = 0 AND input_tokens IS NULL AND output_tokens IS NULL AND latency_ms IS NULL)
    ),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (llm_usage_event_id) REFERENCES llm_usage_events(id),
    FOREIGN KEY (tenant_id, query_request_id) REFERENCES query_requests(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS ai_transfer_receipts_tenant_time_idx
    ON ai_transfer_receipts (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ai_transfer_receipts_query_idx
    ON ai_transfer_receipts (tenant_id, query_request_id);

CREATE TABLE IF NOT EXISTS ai_preflight_confirmations (
    token_id TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ai_preflight_confirmations_expiry_idx
    ON ai_preflight_confirmations (expires_at);

CREATE TABLE IF NOT EXISTS request_quota_windows (
    scope_key TEXT PRIMARY KEY,
    window_number INTEGER NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    active_requests INTEGER NOT NULL CHECK (active_requests >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 10),
    scheduled_at TEXT NOT NULL,
    lease_expires_at TEXT,
    worker_id TEXT,
    result_json TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS background_jobs_claim_idx
    ON background_jobs (status, scheduled_at, created_at);
CREATE INDEX IF NOT EXISTS background_jobs_tenant_idx
    ON background_jobs (tenant_id, data_source_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS background_jobs_active_unique
    ON background_jobs (tenant_id, data_source_id, job_type)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS operational_retention_runs (
    id TEXT PRIMARY KEY,
    cutoff TEXT NOT NULL,
    background_jobs_deleted INTEGER NOT NULL CHECK (background_jobs_deleted >= 0),
    quota_windows_deleted INTEGER NOT NULL CHECK (quota_windows_deleted >= 0),
    actor_id TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authorized_query_definitions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    definition_version INTEGER NOT NULL CHECK (definition_version > 0),
    virtual_schema TEXT NOT NULL,
    virtual_name TEXT NOT NULL,
    description TEXT NOT NULL,
    base_sql TEXT NOT NULL,
    normalized_base_sql TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    allow_filtering INTEGER NOT NULL CHECK (allow_filtering IN (0, 1)),
    allow_aggregation INTEGER NOT NULL CHECK (allow_aggregation IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, data_source_id, definition_version),
    UNIQUE (tenant_id, catalog_version_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS llm_usage_events_tenant_time_idx
    ON llm_usage_events (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS query_requests (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    normalized_sql TEXT,
    referenced_tables_json TEXT NOT NULL,
    referenced_columns_json TEXT NOT NULL,
    validation_issue_codes_json TEXT NOT NULL,
    state TEXT NOT NULL,
    business_concepts_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '[]',
    business_rules_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    provider_id TEXT,
    model_id TEXT,
    llm_usage_event_id TEXT,
    estimated_db_cost REAL,
    estimated_db_rows INTEGER,
    explained_at TEXT,
    parameter_definitions_json TEXT NOT NULL DEFAULT '[]',
    parameter_names_json TEXT NOT NULL DEFAULT '[]',
    parameter_value_hash TEXT,
    output_lineage_json TEXT NOT NULL DEFAULT '[]',
    output_lineage_complete INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT,
    approved_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id) REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (llm_usage_event_id) REFERENCES llm_usage_events(id),
    CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CHECK ((provider_id IS NULL) = (model_id IS NULL)),
    CHECK (estimated_db_cost IS NULL OR estimated_db_cost >= 0),
    CHECK (estimated_db_rows IS NULL OR estimated_db_rows >= 0),
    CHECK (parameter_value_hash IS NULL OR length(parameter_value_hash) = 64),
    CHECK (output_lineage_complete IN (0, 1))
);

CREATE TABLE IF NOT EXISTS corrected_sql_examples (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    question TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    content_classification TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    normalized_sql TEXT NOT NULL,
    referenced_tables_json TEXT NOT NULL,
    referenced_columns_json TEXT NOT NULL,
    business_concepts_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    actor_id TEXT NOT NULL,
    reason TEXT,
    source_query_request_id TEXT,
    supersedes_example_id TEXT,
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_query_request_id)
        REFERENCES query_requests(tenant_id, id),
    FOREIGN KEY (tenant_id, supersedes_example_id)
        REFERENCES corrected_sql_examples(tenant_id, id),
    CHECK (
        (revision = 1 AND supersedes_example_id IS NULL)
        OR (revision > 1 AND supersedes_example_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS security_principals (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    subject TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, subject),
    UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS api_credentials (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    label TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id),
    CHECK (length(token_sha256) = 64)
);

CREATE TABLE IF NOT EXISTS tenant_role_assignments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'data_steward', 'analyst', 'viewer')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, principal_id, role),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS data_source_role_assignments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    data_source_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'data_steward', 'analyst', 'viewer')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, data_source_id, principal_id, role),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id)
        REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS api_credential_revocations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, credential_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, credential_id)
        REFERENCES api_credentials(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS query_feedback_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    query_request_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'rejected', 'corrected')),
    actor_id TEXT NOT NULL,
    reason TEXT,
    corrected_sql_example_id TEXT,
    created_at TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS golden_evaluation_candidates (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    data_source_id TEXT NOT NULL,
    catalog_version_id TEXT NOT NULL,
    corrected_sql_example_id TEXT NOT NULL,
    source_query_request_id TEXT NOT NULL,
    question TEXT NOT NULL,
    normalized_sql TEXT NOT NULL,
    referenced_tables_json TEXT NOT NULL,
    referenced_columns_json TEXT NOT NULL,
    business_concepts_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    content_classification TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, corrected_sql_example_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, catalog_version_id)
        REFERENCES catalog_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, corrected_sql_example_id)
        REFERENCES corrected_sql_examples(tenant_id, id),
    FOREIGN KEY (tenant_id, source_query_request_id)
        REFERENCES query_requests(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS golden_candidate_reviews (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    candidate_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor_id TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, candidate_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES golden_evaluation_candidates(tenant_id, id)
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS operational_retention_runs_no_update
BEFORE UPDATE ON operational_retention_runs
BEGIN
    SELECT RAISE(ABORT, 'operational_retention_runs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS operational_retention_runs_no_delete
BEFORE DELETE ON operational_retention_runs
BEGIN
    SELECT RAISE(ABORT, 'operational_retention_runs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS ai_transfer_receipts_no_update
BEFORE UPDATE ON ai_transfer_receipts
BEGIN
    SELECT RAISE(ABORT, 'ai_transfer_receipts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS ai_transfer_receipts_no_delete
BEFORE DELETE ON ai_transfer_receipts
BEGIN
    SELECT RAISE(ABORT, 'ai_transfer_receipts are append-only');
END;

CREATE TRIGGER IF NOT EXISTS semantic_definitions_no_update
BEFORE UPDATE ON semantic_definitions
BEGIN
    SELECT RAISE(ABORT, 'semantic_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS semantic_definitions_no_delete
BEFORE DELETE ON semantic_definitions
BEGIN
    SELECT RAISE(ABORT, 'semantic_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS business_concept_definitions_no_update
BEFORE UPDATE ON business_concept_definitions
BEGIN
    SELECT RAISE(ABORT, 'business_concept_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS business_concept_definitions_no_delete
BEFORE DELETE ON business_concept_definitions
BEGIN
    SELECT RAISE(ABORT, 'business_concept_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS analytic_semantic_definitions_no_update
BEFORE UPDATE ON analytic_semantic_definitions
BEGIN
    SELECT RAISE(ABORT, 'analytic_semantic_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS analytic_semantic_definitions_no_delete
BEFORE DELETE ON analytic_semantic_definitions
BEGIN
    SELECT RAISE(ABORT, 'analytic_semantic_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS llm_usage_events_no_update
BEFORE UPDATE ON llm_usage_events
BEGIN
    SELECT RAISE(ABORT, 'llm_usage_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS llm_usage_events_no_delete
BEFORE DELETE ON llm_usage_events
BEGIN
    SELECT RAISE(ABORT, 'llm_usage_events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS authorized_query_definitions_no_update
BEFORE UPDATE ON authorized_query_definitions
BEGIN
    SELECT RAISE(ABORT, 'authorized_query_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS authorized_query_definitions_no_delete
BEFORE DELETE ON authorized_query_definitions
BEGIN
    SELECT RAISE(ABORT, 'authorized_query_definitions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS corrected_sql_examples_no_update
BEFORE UPDATE ON corrected_sql_examples
BEGIN
    SELECT RAISE(ABORT, 'corrected_sql_examples are immutable');
END;

CREATE TRIGGER IF NOT EXISTS corrected_sql_examples_no_delete
BEFORE DELETE ON corrected_sql_examples
BEGIN
    SELECT RAISE(ABORT, 'corrected_sql_examples are immutable');
END;

CREATE TRIGGER IF NOT EXISTS query_feedback_events_no_update
BEFORE UPDATE ON query_feedback_events
BEGIN
    SELECT RAISE(ABORT, 'query_feedback_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS security_principals_no_update
BEFORE UPDATE ON security_principals
BEGIN
    SELECT RAISE(ABORT, 'security_principals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS security_principals_no_delete
BEFORE DELETE ON security_principals
BEGIN
    SELECT RAISE(ABORT, 'security_principals are immutable');
END;

CREATE TRIGGER IF NOT EXISTS api_credentials_no_update
BEFORE UPDATE ON api_credentials
BEGIN
    SELECT RAISE(ABORT, 'api_credentials are immutable');
END;

CREATE TRIGGER IF NOT EXISTS api_credentials_no_delete
BEFORE DELETE ON api_credentials
BEGIN
    SELECT RAISE(ABORT, 'api_credentials are immutable');
END;

CREATE TRIGGER IF NOT EXISTS tenant_role_assignments_no_update
BEFORE UPDATE ON tenant_role_assignments
BEGIN
    SELECT RAISE(ABORT, 'tenant_role_assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS tenant_role_assignments_no_delete
BEFORE DELETE ON tenant_role_assignments
BEGIN
    SELECT RAISE(ABORT, 'tenant_role_assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS data_source_role_assignments_no_update
BEFORE UPDATE ON data_source_role_assignments
BEGIN
    SELECT RAISE(ABORT, 'data_source_role_assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS data_source_role_assignments_no_delete
BEFORE DELETE ON data_source_role_assignments
BEGIN
    SELECT RAISE(ABORT, 'data_source_role_assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS api_credential_revocations_no_update
BEFORE UPDATE ON api_credential_revocations
BEGIN
    SELECT RAISE(ABORT, 'api_credential_revocations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS api_credential_revocations_no_delete
BEFORE DELETE ON api_credential_revocations
BEGIN
    SELECT RAISE(ABORT, 'api_credential_revocations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS query_feedback_events_no_delete
BEFORE DELETE ON query_feedback_events
BEGIN
    SELECT RAISE(ABORT, 'query_feedback_events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS golden_evaluation_candidates_no_update
BEFORE UPDATE ON golden_evaluation_candidates
BEGIN
    SELECT RAISE(ABORT, 'golden_evaluation_candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS golden_evaluation_candidates_no_delete
BEFORE DELETE ON golden_evaluation_candidates
BEGIN
    SELECT RAISE(ABORT, 'golden_evaluation_candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS golden_candidate_reviews_no_update
BEFORE UPDATE ON golden_candidate_reviews
BEGIN
    SELECT RAISE(ABORT, 'golden_candidate_reviews are immutable');
END;

CREATE TRIGGER IF NOT EXISTS golden_candidate_reviews_no_delete
BEFORE DELETE ON golden_candidate_reviews
BEGIN
    SELECT RAISE(ABORT, 'golden_candidate_reviews are immutable');
END;

CREATE UNIQUE INDEX IF NOT EXISTS data_sources_tenant_id_unique
    ON data_sources (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS catalog_versions_tenant_id_unique
    ON catalog_versions (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS schema_objects_tenant_id_unique
    ON schema_objects (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS semantic_definitions_tenant_id_unique
    ON semantic_definitions (tenant_id, id);
CREATE INDEX IF NOT EXISTS business_concept_definitions_history_idx
    ON business_concept_definitions
        (tenant_id, data_source_id, concept_key, created_at DESC);
CREATE INDEX IF NOT EXISTS business_concept_resolutions_review_idx
    ON business_concept_resolutions
        (tenant_id, data_source_id, epistemic_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS analytic_semantic_definitions_history_idx
    ON analytic_semantic_definitions
        (tenant_id, data_source_id, asset_kind, asset_key, created_at DESC);
CREATE INDEX IF NOT EXISTS analytic_semantic_resolutions_review_idx
    ON analytic_semantic_resolutions
        (tenant_id, data_source_id, asset_kind, epistemic_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS query_requests_tenant_source_time_idx
    ON query_requests (tenant_id, data_source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS model_pricing_lookup_idx
    ON model_pricing (tenant_id, provider_id, model_id, valid_from DESC);
CREATE INDEX IF NOT EXISTS tenant_budgets_lookup_idx
    ON tenant_budgets (tenant_id, currency, valid_from DESC);
CREATE INDEX IF NOT EXISTS authorized_query_definitions_lookup_idx
    ON authorized_query_definitions (tenant_id, data_source_id, definition_version DESC);
CREATE INDEX IF NOT EXISTS corrected_sql_examples_lookup_idx
    ON corrected_sql_examples
        (tenant_id, data_source_id, normalized_question, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS corrected_sql_examples_root_unique
    ON corrected_sql_examples (tenant_id, data_source_id, normalized_question)
    WHERE supersedes_example_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS corrected_sql_examples_successor_unique
    ON corrected_sql_examples (tenant_id, supersedes_example_id)
    WHERE supersedes_example_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS query_feedback_summary_idx
    ON query_feedback_events (tenant_id, data_source_id, outcome, created_at DESC);
CREATE INDEX IF NOT EXISTS golden_evaluation_candidates_lookup_idx
    ON golden_evaluation_candidates (tenant_id, data_source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS golden_candidate_reviews_decision_idx
    ON golden_candidate_reviews (tenant_id, decision, created_at DESC);
CREATE INDEX IF NOT EXISTS security_principals_tenant_idx
    ON security_principals (tenant_id, created_at, id);
CREATE INDEX IF NOT EXISTS api_credentials_principal_idx
    ON api_credentials (tenant_id, principal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS tenant_role_assignments_lookup_idx
    ON tenant_role_assignments (tenant_id, principal_id, role);
CREATE INDEX IF NOT EXISTS data_source_role_assignments_lookup_idx
    ON data_source_role_assignments (tenant_id, data_source_id, principal_id, role);
