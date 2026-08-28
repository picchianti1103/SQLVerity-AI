#!/bin/sh

if [ -z "${SQLVERITY_DEMO_DB_PASSWORD:-}" ]; then
    echo "SQLVERITY_DEMO_DB_PASSWORD is required" >&2
    return 1 2>/dev/null || exit 1
fi

psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
\getenv demo_password SQLVERITY_DEMO_DB_PASSWORD
SELECT format(
    'CREATE ROLE sqlverity_demo_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
    :'demo_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sqlverity_demo_reader')
\gexec
ALTER ROLE sqlverity_demo_reader
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
    PASSWORD :'demo_password';
SELECT 'CREATE DATABASE sqlverity_demo'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sqlverity_demo')
\gexec
EOSQL
