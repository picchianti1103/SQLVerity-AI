BEGIN;

ALTER TABLE query_requests
    ADD COLUMN parameter_definitions jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN output_lineage jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN output_lineage_complete boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT query_requests_parameter_definitions_array
        CHECK (jsonb_typeof(parameter_definitions) = 'array'),
    ADD CONSTRAINT query_requests_output_lineage_array
        CHECK (jsonb_typeof(output_lineage) = 'array');

COMMIT;
