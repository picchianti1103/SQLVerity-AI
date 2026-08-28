CREATE TABLE accounts (
    id bigint PRIMARY KEY,
    account_code text UNIQUE NOT NULL,
    account_name text NOT NULL,
    account_type text NOT NULL
);

CREATE TABLE invoices (
    id bigint PRIMARY KEY,
    customer_name text NOT NULL,
    issued_on date NOT NULL,
    due_on date NOT NULL,
    status text NOT NULL,
    total_amount numeric(14, 2) NOT NULL
);

CREATE TABLE invoice_lines (
    id bigint PRIMARY KEY,
    invoice_id bigint NOT NULL REFERENCES invoices (id),
    account_id bigint NOT NULL REFERENCES accounts (id),
    description text NOT NULL,
    net_amount numeric(14, 2) NOT NULL,
    tax_amount numeric(14, 2) NOT NULL
);

CREATE TABLE payments (
    id bigint PRIMARY KEY,
    invoice_id bigint NOT NULL REFERENCES invoices (id),
    paid_on date NOT NULL,
    amount numeric(14, 2) NOT NULL,
    method text NOT NULL
);

COMMENT ON COLUMN invoices.total_amount IS
    'Confirmed invoice gross amount, including tax.';
COMMENT ON COLUMN payments.amount IS
    'Confirmed amount received and allocated to an invoice.';
