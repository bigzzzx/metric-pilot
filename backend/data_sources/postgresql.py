"""PostgreSQL metadata discovery using a strictly read-only connection."""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlparse

from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    value = endpoint.strip()
    if not value:
        raise ConnectorUnavailable("PostgreSQL address is required")
    parsed = urlparse(value if "://" in value else f"postgresql://{value}")
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ConnectorUnavailable("PostgreSQL address must look like host:5432 or postgresql://host:5432")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ConnectorUnavailable("Put the database, username and password in their dedicated fields")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ConnectorUnavailable("PostgreSQL port is invalid") from exc
    return parsed.hostname, port


class PostgresConnector:
    def __init__(self, config: ConnectorConfig, connect_factory=None):
        self.config = config
        self._connect_factory = connect_factory

    def _connect(self):
        if not self.config.catalog_name:
            raise ConnectorUnavailable("PostgreSQL database name is required")
        if not self.config.username or not self.config.password:
            raise ConnectorUnavailable("PostgreSQL read-only username and password are required")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise ConnectorUnavailable("PostgreSQL driver is not installed") from exc
        host, port = parse_endpoint(self.config.endpoint)
        connect = self._connect_factory or psycopg.connect
        try:
            return connect(
                host=host,
                port=port,
                dbname=self.config.catalog_name,
                user=self.config.username,
                password=self.config.password,
                connect_timeout=5,
                application_name="data-agent-metadata",
                options="-c default_transaction_read_only=on -c statement_timeout=10000",
                row_factory=dict_row,
            )
        except Exception as exc:
            raise ConnectorUnavailable(f"PostgreSQL connection failed: {exc}") from exc

    def test(self) -> dict:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database() AS database_name, current_user AS username, "
                    "current_setting('transaction_read_only') AS read_only, "
                    "current_setting('server_version') AS server_version"
                )
                row = cursor.fetchone()
        if row["read_only"] != "on":
            raise ConnectorUnavailable("The configured PostgreSQL session is not read-only")
        return {
            "status": "connected",
            "message": f"PostgreSQL {row['server_version']} connected in read-only mode.",
            "database": row["database_name"],
            "username": row["username"],
        }

    def discover(self) -> dict:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT n.nspname AS namespace, c.relname AS name,
                           COALESCE(obj_description(c.oid, 'pg_class'), '') AS description
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p')
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND n.nspname NOT LIKE 'pg_temp_%'
                    ORDER BY n.nspname, c.relname
                    """
                )
                table_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT n.nspname AS namespace, c.relname AS table_name,
                           a.attname AS name, format_type(a.atttypid, a.atttypmod) AS data_type,
                           a.attnum AS ordinal, NOT a.attnotnull AS nullable,
                           COALESCE(col_description(c.oid, a.attnum), '') AS description
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND n.nspname NOT LIKE 'pg_temp_%'
                    ORDER BY n.nspname, c.relname, a.attnum
                    """
                )
                column_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT ns.nspname AS namespace, tbl.relname AS table_name,
                           att.attname AS column_name, keycols.ordinality AS position
                    FROM pg_constraint con
                    JOIN pg_class tbl ON tbl.oid = con.conrelid
                    JOIN pg_namespace ns ON ns.oid = tbl.relnamespace
                    CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS keycols(attnum, ordinality)
                    JOIN pg_attribute att ON att.attrelid = tbl.oid AND att.attnum = keycols.attnum
                    WHERE con.contype = 'p'
                    ORDER BY ns.nspname, tbl.relname, keycols.ordinality
                    """
                )
                primary_key_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT src_ns.nspname AS from_namespace, src.relname AS from_table,
                           src_att.attname AS from_column, dst_ns.nspname AS to_namespace,
                           dst.relname AS to_table, dst_att.attname AS to_column,
                           src_key.ordinality AS position
                    FROM pg_constraint con
                    JOIN pg_class src ON src.oid = con.conrelid
                    JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
                    JOIN pg_class dst ON dst.oid = con.confrelid
                    JOIN pg_namespace dst_ns ON dst_ns.oid = dst.relnamespace
                    CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS src_key(attnum, ordinality)
                    JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS dst_key(attnum, ordinality)
                      ON dst_key.ordinality = src_key.ordinality
                    JOIN pg_attribute src_att ON src_att.attrelid = src.oid AND src_att.attnum = src_key.attnum
                    JOIN pg_attribute dst_att ON dst_att.attrelid = dst.oid AND dst_att.attnum = dst_key.attnum
                    WHERE con.contype = 'f'
                    ORDER BY src_ns.nspname, src.relname, con.conname, src_key.ordinality
                    """
                )
                foreign_key_rows = cursor.fetchall()

        primary_keys: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in primary_key_rows:
            primary_keys[(row["namespace"], row["table_name"])].append(row["column_name"])
        columns: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in column_rows:
            key = (row["namespace"], row["table_name"])
            columns[key].append(
                {
                    "name": row["name"],
                    "type": row["data_type"],
                    "ordinal": row["ordinal"],
                    "nullable": bool(row["nullable"]),
                    "description": row["description"],
                    "is_primary_key": row["name"] in primary_keys[key],
                }
            )
        tables = []
        for row in table_rows:
            key = (row["namespace"], row["name"])
            tables.append(
                {
                    "namespace": row["namespace"],
                    "name": row["name"],
                    "description": row["description"],
                    "primary_key": primary_keys[key],
                    "columns": columns[key],
                }
            )
        relationships = [
            {
                "from_namespace": row["from_namespace"],
                "from_table": row["from_table"],
                "from_column": row["from_column"],
                "to_namespace": row["to_namespace"],
                "to_table": row["to_table"],
                "to_column": row["to_column"],
                "relation": "foreign_key",
                "cardinality": "many_to_one",
                "recommended_join": "LEFT JOIN",
                "verified": True,
            }
            for row in foreign_key_rows
        ]
        return {
            "status": "discovered",
            "tables": tables,
            "relationships": relationships,
            "table_count": len(tables),
            "relationship_count": len(relationships),
        }
