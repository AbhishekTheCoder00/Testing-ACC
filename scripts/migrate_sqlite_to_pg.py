"""
migrate_sqlite_to_pg.py — one-shot copy of the local SQLite database into
PostgreSQL.

Usage (from the acc-connector/ directory):
    set CONN_STRING=host=localhost port=5432 dbname=acc_connector user=postgres password=... connect_timeout=10 sslmode=prefer
    python scripts/migrate_sqlite_to_pg.py

What it does:
  1. Creates the target Postgres database + schema by running init_db().
  2. Copies every table (except app_secrets) from backend/connector.db into the
     Postgres database, preserving primary keys and identity sequences.
  3. Reports row counts per table.

Note: app_secrets is intentionally NOT copied — Databricks credentials should
be re-entered in the Connect Databricks panel on the new backend rather than
inherited from a local dev database.
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SQLITE_DB = ROOT / 'backend' / 'connector.db'

EXCLUDE_TABLES = {'app_secrets'}

TABLES = [
    'acc_tokens',
    'acc_config',
    'dbx_credentials',
    'dbx_tokens',
    'bootstrap_state',
    'watermarks',
    'sync_runs',
    'm2m_config',
    'm2m_bootstrap_state',
    'm2m_sync_runs',
    'm2m_watermarks',
]


def _pg_connect() -> object:
    import psycopg

    conn_string = os.getenv('CONN_STRING', '').strip()
    if not conn_string:
        sys.exit('CONN_STRING is not set — set it to the target PostgreSQL DSN first.')
    return psycopg.connect(conn_string)


def main() -> None:
    if not SQLITE_DB.exists():
        sys.exit(f'SQLite database not found: {SQLITE_DB}')

    # 1. Ensure the target DB + schema exist.
    from backend.repositories.state import database
    database.init_db()

    src = sqlite3.connect(str(SQLITE_DB))
    src.row_factory = sqlite3.Row
    dst = _pg_connect()
    dst.autocommit = True

    try:
        for table in TABLES:
            rows = src.execute(f'SELECT * FROM {table}').fetchall()
            if not rows:
                print(f'{table:24s} 0 rows (skip)')
                continue
            columns = list(rows[0].keys())
            placeholders = ','.join(['%s'] * len(columns))
            col_sql = ','.join(f'"{c}"' for c in columns)
            sql = (
                f'INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) '
                f'ON CONFLICT DO NOTHING'
            )
            dst.executemany(sql, [tuple(r[c] for c in columns) for r in rows])

            # Advance identity sequences past the copied max ids.
            id_col = {
                'sync_runs': 'run_id',
                'm2m_config': 'config_id',
                'm2m_sync_runs': 'run_id',
            }.get(table)
            if id_col:
                max_id = max(r[id_col] for r in rows if r[id_col] is not None)
                dst.execute(
                    f'SELECT setval(pg_get_serial_sequence(\'{table}\', \'{id_col}\'), %s, true)',
                    (max_id,),
                )
            print(f'{table:24s} {len(rows)} rows copied')
    finally:
        src.close()
        dst.close()

    print('\nDone. Databricks credentials are not copied — enter them in the')
    print('Connect Databricks panel after pointing the app at PostgreSQL.')


if __name__ == '__main__':
    main()
