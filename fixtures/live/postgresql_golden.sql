CREATE SCHEMA IF NOT EXISTS commerce;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS support;

CREATE TABLE IF NOT EXISTS commerce.customers (
    id bigint PRIMARY KEY,
    country text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS commerce.products (
    id bigint PRIMARY KEY,
    category text NOT NULL,
    active boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS commerce.orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES commerce.customers(id),
    status text NOT NULL,
    ordered_at timestamptz NOT NULL,
    total_amount numeric(14, 2) NOT NULL,
    net_amount numeric(14, 2) NOT NULL
);
CREATE TABLE IF NOT EXISTS commerce.order_items (
    order_id bigint NOT NULL REFERENCES commerce.orders(id),
    product_id bigint NOT NULL REFERENCES commerce.products(id),
    quantity integer NOT NULL,
    net_amount numeric(14, 2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE IF NOT EXISTS finance.accounts (
    id bigint PRIMARY KEY,
    region text NOT NULL,
    active boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS finance.invoices (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    status text NOT NULL,
    invoice_date date NOT NULL,
    net_amount numeric(14, 2) NOT NULL
);
CREATE TABLE IF NOT EXISTS finance.invoice_lines (
    invoice_id bigint NOT NULL REFERENCES finance.invoices(id),
    product_id bigint NOT NULL,
    net_amount numeric(14, 2) NOT NULL,
    PRIMARY KEY (invoice_id, product_id)
);
CREATE TABLE IF NOT EXISTS finance.payments (
    id bigint PRIMARY KEY,
    invoice_id bigint NOT NULL REFERENCES finance.invoices(id),
    status text NOT NULL,
    paid_at timestamptz NOT NULL,
    amount numeric(14, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS support.tickets (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    status text NOT NULL,
    priority text NOT NULL,
    opened_at timestamptz NOT NULL,
    resolved_at timestamptz
);
CREATE TABLE IF NOT EXISTS support.ticket_events (
    ticket_id bigint NOT NULL REFERENCES support.tickets(id),
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS support.agents (
    id bigint PRIMARY KEY,
    team text NOT NULL,
    active boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS support.satisfaction (
    ticket_id bigint PRIMARY KEY REFERENCES support.tickets(id),
    score numeric(4, 2) NOT NULL,
    submitted_at timestamptz NOT NULL
);

TRUNCATE
    commerce.order_items,
    commerce.orders,
    commerce.products,
    commerce.customers,
    finance.payments,
    finance.invoice_lines,
    finance.invoices,
    finance.accounts,
    support.satisfaction,
    support.ticket_events,
    support.tickets,
    support.agents
CASCADE;

INSERT INTO commerce.customers VALUES
    (1, 'IT', 'active', '2026-01-10T10:00:00Z'),
    (2, 'DE', 'active', '2026-02-12T10:00:00Z'),
    (3, 'IT', 'inactive', '2025-11-03T10:00:00Z');
INSERT INTO commerce.products VALUES
    (10, 'hardware', true),
    (11, 'software', true),
    (12, 'hardware', false);
INSERT INTO commerce.orders VALUES
    (100, 1, 'completed', '2026-03-01T09:00:00Z', 120.00, 100.00),
    (101, 1, 'cancelled', '2026-03-02T09:00:00Z', 50.00, 45.00),
    (102, 2, 'completed', '2026-04-05T09:00:00Z', 240.00, 200.00),
    (103, 3, 'pending', '2025-12-20T09:00:00Z', 80.00, 70.00);
INSERT INTO commerce.order_items VALUES
    (100, 10, 2, 80.00),
    (100, 11, 1, 20.00),
    (102, 10, 3, 150.00),
    (102, 11, 2, 50.00);

INSERT INTO finance.accounts VALUES
    (1, 'south', true),
    (2, 'north', true),
    (3, 'south', false);
INSERT INTO finance.invoices VALUES
    (200, 1, 'POSTED', '2026-01-15', 100.00),
    (201, 1, 'OPEN', '2026-02-20', 80.00),
    (202, 2, 'POSTED', '2026-03-10', 200.00),
    (203, 3, 'OPEN', '2025-12-01', 50.00);
INSERT INTO finance.invoice_lines VALUES
    (200, 10, 60.00),
    (200, 11, 40.00),
    (202, 10, 200.00);
INSERT INTO finance.payments VALUES
    (300, 200, 'SETTLED', '2026-01-20T09:00:00Z', 100.00),
    (301, 202, 'SETTLED', '2026-03-15T09:00:00Z', 200.00),
    (302, 201, 'FAILED', '2026-02-25T09:00:00Z', 80.00);

INSERT INTO support.tickets VALUES
    (400, 1, 'OPEN', 'HIGH', '2026-01-01T08:00:00Z', NULL),
    (401, 2, 'RESOLVED', 'LOW', '2026-01-05T08:00:00Z', '2026-01-06T08:00:00Z'),
    (402, 3, 'RESOLVED', 'HIGH', '2026-02-01T08:00:00Z', '2026-02-03T20:00:00Z');
INSERT INTO support.ticket_events VALUES
    (400, 'opened', '2026-01-01T08:00:00Z'),
    (401, 'opened', '2026-01-05T08:00:00Z'),
    (401, 'resolved', '2026-01-06T08:00:00Z'),
    (402, 'resolved', '2026-02-03T20:00:00Z');
INSERT INTO support.agents VALUES
    (1, 'tier-1', true),
    (2, 'tier-1', true),
    (3, 'tier-2', false);
INSERT INTO support.satisfaction VALUES
    (401, 4.50, '2026-01-07T08:00:00Z'),
    (402, 3.50, '2026-02-04T08:00:00Z');
