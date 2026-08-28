CREATE TABLE agents (
    id bigint PRIMARY KEY,
    display_name text NOT NULL,
    team text NOT NULL,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE tickets (
    id bigint PRIMARY KEY,
    assigned_agent_id bigint REFERENCES agents (id),
    priority text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    resolved_at timestamptz
);

CREATE TABLE ticket_events (
    id bigint PRIMARY KEY,
    ticket_id bigint NOT NULL REFERENCES tickets (id),
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    actor_type text NOT NULL
);

CREATE TABLE satisfaction (
    id bigint PRIMARY KEY,
    ticket_id bigint UNIQUE NOT NULL REFERENCES tickets (id),
    score smallint NOT NULL CHECK (score BETWEEN 1 AND 5),
    submitted_at timestamptz NOT NULL
);

COMMENT ON COLUMN tickets.resolved_at IS
    'Timestamp at which the ticket first entered a resolved state.';
COMMENT ON COLUMN satisfaction.score IS
    'Customer satisfaction score from 1 (lowest) to 5 (highest).';
