#!/bin/sh
set -eu

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=reader_password="$DATA_AGENT_READER_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE data_agent_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'reader_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'data_agent_reader') \gexec
ALTER ROLE data_agent_reader SET default_transaction_read_only = on;
ALTER ROLE data_agent_reader SET statement_timeout = '30s';
SQL
