CREATE TABLE customers (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    country_code char(2) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE products (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    category text NOT NULL,
    unit_price numeric(12, 2) NOT NULL,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE orders (
    id bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers (id),
    status text NOT NULL,
    ordered_at timestamptz NOT NULL,
    total_amount numeric(14, 2) NOT NULL
);

CREATE TABLE order_items (
    id bigint PRIMARY KEY,
    order_id bigint NOT NULL REFERENCES orders (id),
    product_id bigint NOT NULL REFERENCES products (id),
    quantity integer NOT NULL,
    unit_price numeric(12, 2) NOT NULL
);

COMMENT ON COLUMN orders.total_amount IS
    'Confirmed gross order value in the transaction currency.';
COMMENT ON COLUMN orders.status IS
    'Lifecycle state: pending, paid, shipped, cancelled, or refunded.';
