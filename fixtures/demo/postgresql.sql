\connect sqlverity_demo

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.customers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    country_code char(2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS demo.orders (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES demo.customers(id),
    ordered_at timestamptz NOT NULL,
    status text NOT NULL,
    total_amount numeric(12, 2) NOT NULL CHECK (total_amount >= 0)
);

COMMENT ON TABLE demo.customers IS 'Synthetic customers used by the SQLVerity AI quickstart.';
COMMENT ON TABLE demo.orders IS 'Synthetic commerce orders used by the SQLVerity AI quickstart.';
COMMENT ON COLUMN demo.orders.total_amount IS 'Order total in EUR.';

INSERT INTO demo.customers (id, name, country_code, created_at)
OVERRIDING SYSTEM VALUE
VALUES
    (1, 'Ada Market', 'IT', '2026-01-10T09:00:00Z'),
    (2, 'Northwind Lab', 'DE', '2026-02-12T10:00:00Z'),
    (3, 'Blue River', 'FR', '2026-03-05T11:00:00Z')
ON CONFLICT (id) DO NOTHING;

INSERT INTO demo.orders (id, customer_id, ordered_at, status, total_amount)
OVERRIDING SYSTEM VALUE
VALUES
    (1, 1, '2026-06-01T08:30:00Z', 'paid', 125.50),
    (2, 1, '2026-06-08T14:10:00Z', 'shipped', 89.00),
    (3, 2, '2026-06-11T12:00:00Z', 'paid', 410.25),
    (4, 3, '2026-06-19T16:45:00Z', 'cancelled', 55.00)
ON CONFLICT (id) DO NOTHING;

SELECT setval(
    pg_get_serial_sequence('demo.customers', 'id'),
    COALESCE(MAX(id), 1),
    true
) FROM demo.customers;
SELECT setval(
    pg_get_serial_sequence('demo.orders', 'id'),
    COALESCE(MAX(id), 1),
    true
) FROM demo.orders;

REVOKE ALL ON SCHEMA demo FROM PUBLIC;
GRANT CONNECT ON DATABASE sqlverity_demo TO sqlverity_demo_reader;
GRANT USAGE ON SCHEMA demo TO sqlverity_demo_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA demo TO sqlverity_demo_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA demo
    GRANT SELECT ON TABLES TO sqlverity_demo_reader;
