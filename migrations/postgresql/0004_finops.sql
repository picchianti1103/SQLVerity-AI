BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE llm_usage_events
    ADD COLUMN cached_input_tokens bigint NOT NULL DEFAULT 0
        CHECK (cached_input_tokens >= 0),
    ADD COLUMN currency text,
    ADD COLUMN pricing_id uuid;

ALTER TABLE llm_usage_events
    ADD CONSTRAINT llm_usage_events_cached_input_check
        CHECK (cached_input_tokens <= input_tokens),
    ADD CONSTRAINT llm_usage_events_currency_check
        CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$');

CREATE TABLE model_pricing (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    provider_id text NOT NULL,
    model_id text NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    currency text NOT NULL,
    token_unit bigint NOT NULL CHECK (token_unit > 0),
    input_price_per_unit numeric NOT NULL CHECK (input_price_per_unit >= 0),
    cached_input_price_per_unit numeric CHECK (cached_input_price_per_unit >= 0),
    output_price_per_unit numeric NOT NULL CHECK (output_price_per_unit >= 0),
    batch_discount numeric NOT NULL DEFAULT 0 CHECK (batch_discount >= 0 AND batch_discount < 1),
    notes text,
    source_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (currency ~ '^[A-Z]{3}$')
);

ALTER TABLE llm_usage_events
    ADD CONSTRAINT llm_usage_events_pricing_fk
        FOREIGN KEY (pricing_id) REFERENCES model_pricing(id);

CREATE INDEX model_pricing_lookup_idx
    ON model_pricing (tenant_id, provider_id, model_id, valid_from DESC);

ALTER TABLE model_pricing
    ADD CONSTRAINT model_pricing_no_overlap
        EXCLUDE USING gist (
            tenant_id WITH =,
            provider_id WITH =,
            model_id WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        );

CREATE TABLE tenant_budgets (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    currency text NOT NULL,
    amount numeric NOT NULL CHECK (amount > 0),
    period text NOT NULL CHECK (period = 'monthly'),
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (currency ~ '^[A-Z]{3}$')
);

CREATE INDEX tenant_budgets_lookup_idx
    ON tenant_budgets (tenant_id, currency, valid_from DESC);

ALTER TABLE tenant_budgets
    ADD CONSTRAINT tenant_budgets_no_overlap
        EXCLUDE USING gist (
            tenant_id WITH =,
            currency WITH =,
            period WITH =,
            tstzrange(valid_from, valid_to, '[)') WITH &&
        );

CREATE TABLE execution_cost_policies (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    data_source_id uuid NOT NULL,
    max_total_cost double precision CHECK (max_total_cost > 0),
    max_estimated_rows bigint CHECK (max_estimated_rows > 0),
    require_explain boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, data_source_id),
    FOREIGN KEY (tenant_id, data_source_id) REFERENCES data_sources(tenant_id, id),
    CHECK (max_total_cost IS NOT NULL OR max_estimated_rows IS NOT NULL)
);

COMMIT;
