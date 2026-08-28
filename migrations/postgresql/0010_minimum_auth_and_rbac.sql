BEGIN;

CREATE TABLE security_principals (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    subject text NOT NULL,
    display_name text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, subject),
    UNIQUE (tenant_id, id)
);

CREATE TABLE api_credentials (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    label text NOT NULL,
    token_sha256 text NOT NULL UNIQUE CHECK (length(token_sha256) = 64),
    expires_at timestamptz,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id),
    CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE TABLE tenant_role_assignments (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    principal_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'data_steward', 'analyst', 'viewer')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, principal_id, role),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id)
);

CREATE TABLE data_source_role_assignments (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    data_source_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'data_steward', 'analyst', 'viewer')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, data_source_id, principal_id, role),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    FOREIGN KEY (tenant_id, principal_id)
        REFERENCES security_principals(tenant_id, id)
);

CREATE TABLE api_credential_revocations (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    actor_id text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, credential_id),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, credential_id)
        REFERENCES api_credentials(tenant_id, id)
);

CREATE INDEX security_principals_tenant_idx
    ON security_principals (tenant_id, created_at, id);
CREATE INDEX api_credentials_principal_idx
    ON api_credentials (tenant_id, principal_id, created_at DESC);
CREATE INDEX tenant_role_assignments_lookup_idx
    ON tenant_role_assignments (tenant_id, principal_id, role);
CREATE INDEX data_source_role_assignments_lookup_idx
    ON data_source_role_assignments (tenant_id, data_source_id, principal_id, role);

CREATE FUNCTION reject_security_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER security_principals_no_update_or_delete
BEFORE UPDATE OR DELETE ON security_principals
FOR EACH ROW EXECUTE FUNCTION reject_security_evidence_mutation();
CREATE TRIGGER api_credentials_no_update_or_delete
BEFORE UPDATE OR DELETE ON api_credentials
FOR EACH ROW EXECUTE FUNCTION reject_security_evidence_mutation();
CREATE TRIGGER tenant_role_assignments_no_update_or_delete
BEFORE UPDATE OR DELETE ON tenant_role_assignments
FOR EACH ROW EXECUTE FUNCTION reject_security_evidence_mutation();
CREATE TRIGGER data_source_role_assignments_no_update_or_delete
BEFORE UPDATE OR DELETE ON data_source_role_assignments
FOR EACH ROW EXECUTE FUNCTION reject_security_evidence_mutation();
CREATE TRIGGER api_credential_revocations_no_update_or_delete
BEFORE UPDATE OR DELETE ON api_credential_revocations
FOR EACH ROW EXECUTE FUNCTION reject_security_evidence_mutation();

COMMIT;
