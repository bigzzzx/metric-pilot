#!/bin/sh
set -eu

cd "$(dirname "$0")"
set -a
. ./.env
set +a
docker inspect data-agent-postgres --format '{{.State.Health.Status}}' | grep -qx healthy
docker inspect postgres --format '{{.State.Health.Status}}' | grep -qx healthy
docker port data-agent-postgres 5432/tcp | grep -qx '127.0.0.1:5433'

admin_psql() {
  docker exec data-agent-postgres psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -Atc "$1"
}

reader_psql() {
  docker exec data-agent-postgres sh -ec \
    'PGPASSWORD="$DATA_AGENT_READER_PASSWORD" psql -v ON_ERROR_STOP=1 --username data_agent_reader --dbname "$POSTGRES_DB" -Atc "$1"' \
    sh "$1"
}

[ "$(admin_psql "SELECT count(*) FROM information_schema.schemata WHERE schema_name IN ('ods','dwd','dws','ads')")" = 4 ]
[ "$(admin_psql "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('ods','dwd','dws','ads') AND table_type='BASE TABLE'")" = 20 ]
[ "$(admin_psql "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname IN ('ods','dwd') AND c.contype='f'")" -ge 8 ]
[ "$(admin_psql "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('ods','dwd','dws','ads') AND c.relkind='r' AND obj_description(c.oid,'pg_class') IS NOT NULL")" = 20 ]
[ "$(admin_psql "SELECT count(*) FROM information_schema.columns c WHERE c.table_schema IN ('ods','dwd','dws','ads') AND col_description(format('%I.%I',c.table_schema,c.table_name)::regclass::oid,c.ordinal_position) IS NULL")" = 0 ]
[ "$(admin_psql 'SELECT count(*) FROM ods.orders')" = 1200 ]
[ "$(admin_psql 'SELECT count(*) FROM ods.order_items')" = 2400 ]
[ "$(admin_psql 'SELECT count(*) FROM ods.campaign_events')" = 1500 ]
[ "$(reader_psql "SELECT current_setting('transaction_read_only')")" = on ]
[ "$(reader_psql 'SELECT count(*) FROM ads.daily_trade')" -gt 0 ]

if reader_psql "INSERT INTO ads.user_retention VALUES ('2027-01-01','2027-01-01',1,1,1)" >/dev/null 2>&1; then
  echo 'reader write unexpectedly succeeded' >&2
  exit 1
fi

printf 'schemas=4 tables=20 column_comments=complete orders=1200 order_items=2400 campaign_events=1500 reader=read-only\n'
