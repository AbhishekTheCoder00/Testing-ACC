"""
database.py — persistence layer with a dual backend.

  - SQLite   : used when CONN_STRING is unset (local dev, zero config).
  - Postgres : used when CONN_STRING is set (libpq key=value DSN, e.g.
               "host=localhost port=5432 dbname=acc_connector user=postgres
                password=... connect_timeout=10 sslmode=prefer").

Repos keep calling ``with _conn() as con:`` — the returned object exposes the
same ``execute`` / ``executemany`` API with ``?`` placeholders on both backends
(the Postgres adapter translates ``?`` -> ``%s`` for psycopg).

On Postgres the database itself is auto-created on startup if it does not
exist yet, then all tables are created with ``IF NOT EXISTS`` so every restart
is a no-op once the schema is in place.
"""
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'connector.db')
CONN_STRING = os.getenv('CONN_STRING', '').strip()

_pool = None


def _is_pg() -> bool:
    return bool(CONN_STRING)


def _ensure_database() -> None:
    """Create the target database on the Postgres server if it is missing."""
    if not _is_pg():
        return
    import psycopg

    params = psycopg.conninfo.conninfo_to_dict(CONN_STRING)
    dbname = params.get('dbname') or 'acc_connector'
    if dbname in ('postgres', 'template1'):
        return
    maint = dict(params)
    maint['dbname'] = 'postgres'
    conn = psycopg.connect(**maint, autocommit=True)
    try:
        exists = conn.execute(
            'SELECT 1 FROM pg_database WHERE datname = %s', (dbname,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        conn.close()


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            CONN_STRING,
            min_size=1,
            max_size=8,
            kwargs={'row_factory': dict_row},
            open=True,
        )
    return _pool


class _CompatConn:
    """sqlite3-compatible facade over a psycopg connection (PG backend only).

    Translates ``?`` placeholders to ``%s`` so repository SQL stays shared
    with the SQLite backend. Rows come back as dicts (dict_row) so the
    ``row['col']`` / ``dict(row)`` access used by the repos keeps working.
    """

    def __init__(self, pool):
        self._pool = pool
        self._ctx = pool.connection()
        self._raw = None

    def _sql(self, sql):
        return sql.replace('?', '%s')

    def execute(self, sql, params=None):
        if params is None:
            params = ()
        return self._raw.execute(self._sql(sql), params)

    def executemany(self, sql, seq):
        return self._raw.executemany(self._sql(sql), seq)

    def executescript(self, script):
        for stmt in script.split(';'):
            stmt = stmt.strip()
            if stmt:
                self._raw.execute(stmt)

    def __enter__(self):
        self._raw = self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


def _conn():
    if _is_pg():
        return _CompatConn(_get_pool())
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# Schema — SQLite and Postgres
# ---------------------------------------------------------------------------

_OK_SCHEMA_BASE = '''
    CREATE TABLE IF NOT EXISTS catalog_claims (
        claim_id                %ID%,
        workspace_key           TEXT NOT NULL,
        catalog_name            TEXT NOT NULL,
        owner_user_id           TEXT NOT NULL,
        snapshot_pipeline_id    TEXT,
        cdc_pipeline_id         TEXT,
        snapshot_pipeline_name  TEXT,
        cdc_pipeline_name       TEXT,
        snapshot_workflow_id    %INT%,
        cdc_workflow_id         %INT%,
        needs_reprovision       %INT% NOT NULL DEFAULT 0,
        created_at              %REAL% NOT NULL,
        released_at             %REAL%
    );

    CREATE UNIQUE INDEX IF NOT EXISTS ux_catalog_claims_active
        ON catalog_claims (workspace_key, catalog_name)
        WHERE released_at IS NULL;
'''

# ---------------------------------------------------------------------------
# Phase 1 — multi-tenant M2M schema (FR-05).
#
# Hub-scoped tenancy: one tenant per hub_id, one SSA robot per hub, one connection
# per (hub, project, workspace, catalog). The UNIQUE constraints below are load
# bearing, not decoration — they are what makes provisioning idempotent under two
# admins clicking at once (FR-02 FR-28, FR-05 §6.1).
#
# Deliberate deviations from FR-05, recorded in ADR.md (Phase 1):
#   * `connection_sync_runs` / `connection_watermarks` — FR-05 §5.8-5.9 call these
#     `sync_runs` / `watermarks`, but both names are already taken by load-bearing
#     user-scoped U2M tables and SQLite has no schemas to separate them into.
#   * Timestamps are epoch REAL / DOUBLE PRECISION and JSON is TEXT, matching the
#     rest of this file rather than FR-05's TIMESTAMPTZ / JSONB.
#   * Booleans are %INT% 1/0 so one query shape works on both backends.
#   * `connections.auto_cdc_next_run_at` replaces FR-06's cron string: the scheduler
#     tick in FR-03 §11.4 evaluates no cron, so storing one would be decorative.
#   * No (workspace, catalog) exclusivity index here — `catalog_claims` above already
#     enforces it, and reusing it also blocks a U2M/M2M cross-path collision.
# ---------------------------------------------------------------------------

_M2M_SCHEMA_BASE = '''
    CREATE TABLE IF NOT EXISTS aps_apps (
        app_ref           TEXT PRIMARY KEY,
        client_id         TEXT NOT NULL UNIQUE,
        client_secret_ref TEXT NOT NULL,
        max_robots        %INT% NOT NULL DEFAULT 10,
        display_name      TEXT,
        is_active         %INT% NOT NULL DEFAULT 1,
        created_at        %REAL% NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tenants (
        tenant_id         TEXT PRIMARY KEY,
        hub_id            TEXT NOT NULL UNIQUE,
        aps_app_ref       TEXT NOT NULL REFERENCES aps_apps(app_ref),
        acc_account_id    TEXT,
        org_id            TEXT,
        hub_name          TEXT,
        onboarding_status TEXT NOT NULL DEFAULT 'pending_whitelist',
        last_error        TEXT,
        ssa_verified_at   %REAL%,
        -- Orphan adoption: APS creates the robot before we can store its key, so the id is
        -- recorded here first. If the vault write or the credential insert then fails, a retry
        -- adopts this robot instead of burning a second service-account slot on the same hub.
        pending_service_account_id TEXT,
        pending_robot_email        TEXT,
        -- Cross-worker mutex for robot creation. An in-process lock is not enough: two admins
        -- can land on different gunicorn workers, and each would call POST /service-accounts,
        -- burning two of the ten slots for one hub (FR-02 FR-28).
        provisioning_claimed_at    %REAL%,
        created_at        %REAL% NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tenant_users (
        tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        aps_user_id TEXT NOT NULL,
        role        TEXT NOT NULL DEFAULT 'hub_admin',
        created_at  %REAL% NOT NULL,
        PRIMARY KEY (tenant_id, aps_user_id)
    );

    CREATE TABLE IF NOT EXISTS ssa_credentials (
        tenant_id          TEXT PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        hub_id             TEXT NOT NULL UNIQUE,
        aps_app_ref        TEXT NOT NULL REFERENCES aps_apps(app_ref),
        service_account_id TEXT NOT NULL UNIQUE,
        robot_email        TEXT NOT NULL,
        key_id             TEXT NOT NULL,
        private_key_ref    TEXT NOT NULL,
        created_at         %REAL% NOT NULL,
        rotated_at         %REAL%
    );

    CREATE TABLE IF NOT EXISTS connections (
        connection_id        TEXT PRIMARY KEY,
        tenant_id            TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
        hub_id               TEXT NOT NULL,
        project_id           TEXT NOT NULL,
        project_name         TEXT,
        dbx_workspace_url    TEXT NOT NULL,
        catalog              TEXT NOT NULL,
        onboarding_status    TEXT NOT NULL DEFAULT 'pending_custom_integration',
        snapshot_pipeline_id TEXT,
        cdc_pipeline_id      TEXT,
        cdc_enabled          %INT% NOT NULL DEFAULT 0,
        auto_cdc_next_run_at %REAL%,
        last_error           TEXT,
        created_at           %REAL% NOT NULL,
        UNIQUE (hub_id, project_id, dbx_workspace_url, catalog)
    );

    CREATE TABLE IF NOT EXISTS connection_dbx_credentials (
        connection_id     TEXT PRIMARY KEY REFERENCES connections(connection_id) ON DELETE CASCADE,
        workspace_url     TEXT NOT NULL,
        client_id         TEXT NOT NULL,
        client_secret_ref TEXT NOT NULL,
        validated_at      %REAL%
    );

    CREATE TABLE IF NOT EXISTS connection_bootstrap (
        connection_id        TEXT PRIMARY KEY REFERENCES connections(connection_id) ON DELETE CASCADE,
        bronze_job_id        %INT%,
        silver_job_id        %INT%,
        notebook_folder      TEXT,
        volume_path          TEXT,
        warehouse_id         TEXT,
        catalog_name         TEXT,
        snapshot_workflow_id %INT%,
        cdc_workflow_id      %INT%,
        download_job_id      %INT%,
        catalog_claim_id     %INT%,
        bootstrapped_at      %REAL%
    );

    CREATE TABLE IF NOT EXISTS connection_sync_runs (
        run_id        %ID%,
        connection_id TEXT NOT NULL REFERENCES connections(connection_id) ON DELETE CASCADE,
        trigger_type  TEXT NOT NULL DEFAULT 'manual',
        mode          TEXT NOT NULL,
        state         TEXT NOT NULL DEFAULT 'pending',
        phase         TEXT,
        record_counts TEXT,
        files_count   %INT%,
        bronze_run_id %INT%,
        error         TEXT,
        started_at    %REAL% NOT NULL,
        updated_at    %REAL% NOT NULL
    );

    CREATE TABLE IF NOT EXISTS connection_watermarks (
        connection_id TEXT NOT NULL REFERENCES connections(connection_id) ON DELETE CASCADE,
        data_type     TEXT NOT NULL DEFAULT 'data_connector',
        last_sync     %REAL% NOT NULL,
        PRIMARY KEY (connection_id, data_type)
    );

    CREATE INDEX IF NOT EXISTS idx_tenants_aps_app_ref ON tenants(aps_app_ref);
    CREATE INDEX IF NOT EXISTS idx_tenant_users_user ON tenant_users(aps_user_id);
    CREATE INDEX IF NOT EXISTS idx_ssa_credentials_aps_app_ref ON ssa_credentials(aps_app_ref);
    CREATE INDEX IF NOT EXISTS idx_connections_tenant_id ON connections(tenant_id);
    CREATE INDEX IF NOT EXISTS idx_connections_hub_ready ON connections (hub_id)
        WHERE onboarding_status = 'ready';
    CREATE INDEX IF NOT EXISTS idx_connection_sync_runs_state
        ON connection_sync_runs (connection_id, state);
'''

_SQLITE_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS acc_tokens (
        user_id        TEXT PRIMARY KEY,
        access_token   TEXT NOT NULL,
        refresh_token  TEXT NOT NULL,
        expires_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS acc_config (
        user_id        TEXT PRIMARY KEY,
        hub_id         TEXT NOT NULL,
        hub_name       TEXT,
        project_id     TEXT NOT NULL,
        project_name   TEXT,
        folder_id      TEXT,
        folder_name    TEXT,
        acc_account_id TEXT,
        saved_at       REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dbx_credentials (
        user_id       TEXT PRIMARY KEY,
        workspace_url TEXT NOT NULL,
        pat_encrypted TEXT NOT NULL,
        saved_at      REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dbx_tokens (
        user_id        TEXT PRIMARY KEY,
        workspace_url  TEXT NOT NULL,
        cloud_provider TEXT NOT NULL DEFAULT 'unknown',
        access_token   TEXT NOT NULL,
        refresh_token  TEXT,
        expires_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bootstrap_state (
        user_id          TEXT PRIMARY KEY,
        bronze_job_id    INTEGER,
        silver_job_id    INTEGER,
        notebook_folder  TEXT,
        volume_path      TEXT,
        compute_type     TEXT,
        warehouse_id     TEXT,
        bootstrapped_at  REAL,
        download_job_id  INTEGER
    );

    CREATE TABLE IF NOT EXISTS watermarks (
        user_id    TEXT NOT NULL,
        project_id TEXT NOT NULL,
        data_type  TEXT NOT NULL,
        last_sync  REAL NOT NULL,
        PRIMARY KEY (user_id, project_id, data_type)
    );

    CREATE TABLE IF NOT EXISTS sync_runs (
        run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        project_id      TEXT NOT NULL,
        data_types      TEXT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'pending',
        record_counts   TEXT,
        skipped_modules TEXT,
        bronze_run_id   INTEGER,
        silver_run_id   INTEGER,
        error           TEXT,
        started_at      REAL NOT NULL,
        updated_at      REAL NOT NULL,
        user_run_seq    INTEGER
    );

    CREATE TABLE IF NOT EXISTS m2m_config (
        config_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        workspace_url    TEXT NOT NULL,
        acc_account_id   TEXT NOT NULL,
        hub_id           TEXT NOT NULL,
        hub_name         TEXT,
        project_id       TEXT NOT NULL,
        project_name     TEXT,
        catalog_name     TEXT NOT NULL,
        validated_at     REAL,
        saved_at         REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS m2m_bootstrap_state (
        config_id          INTEGER PRIMARY KEY,
        bronze_job_id      INTEGER,
        silver_job_id      INTEGER,
        notebook_folder    TEXT,
        volume_path        TEXT,
        warehouse_id       TEXT,
        catalog_name       TEXT,
        bronze_pipeline_id TEXT,
        cdc_pipeline_id    TEXT,
        bootstrapped_at    REAL
    );

    CREATE TABLE IF NOT EXISTS m2m_sync_runs (
        run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        config_id       INTEGER NOT NULL,
        state           TEXT NOT NULL DEFAULT 'pending',
        record_counts   TEXT,
        bronze_run_id   INTEGER,
        silver_run_id   INTEGER,
        error           TEXT,
        started_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS m2m_watermarks (
        config_id  INTEGER NOT NULL,
        data_type  TEXT NOT NULL DEFAULT 'data_connector',
        last_sync  REAL NOT NULL,
        PRIMARY KEY (config_id, data_type)
    );

    CREATE TABLE IF NOT EXISTS app_secrets (
        user_id                  TEXT PRIMARY KEY,
        DATABRICKS_CLIENT_ID     TEXT,
        DATABRICKS_CLIENT_SECRET TEXT,
        DATABRICKS_WORKSPACE_URL TEXT,
        updated_at               REAL NOT NULL
    );
''' + (_OK_SCHEMA_BASE + _M2M_SCHEMA_BASE) \
        .replace('%ID%', 'INTEGER PRIMARY KEY AUTOINCREMENT') \
        .replace('%INT%', 'INTEGER') \
        .replace('%REAL%', 'REAL')

_PG_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS acc_tokens (
        user_id        TEXT PRIMARY KEY,
        access_token   TEXT NOT NULL,
        refresh_token  TEXT NOT NULL,
        expires_at     DOUBLE PRECISION NOT NULL,
        updated_at     DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS acc_config (
        user_id        TEXT PRIMARY KEY,
        hub_id         TEXT NOT NULL,
        hub_name       TEXT,
        project_id     TEXT NOT NULL,
        project_name   TEXT,
        folder_id      TEXT,
        folder_name    TEXT,
        acc_account_id TEXT,
        saved_at       DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dbx_credentials (
        user_id       TEXT PRIMARY KEY,
        workspace_url TEXT NOT NULL,
        pat_encrypted TEXT NOT NULL,
        saved_at      DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS dbx_tokens (
        user_id        TEXT PRIMARY KEY,
        workspace_url  TEXT NOT NULL,
        cloud_provider TEXT NOT NULL DEFAULT 'unknown',
        access_token   TEXT NOT NULL,
        refresh_token  TEXT,
        expires_at     DOUBLE PRECISION NOT NULL,
        updated_at     DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS bootstrap_state (
        user_id          TEXT PRIMARY KEY,
        bronze_job_id    BIGINT,
        silver_job_id    BIGINT,
        notebook_folder  TEXT,
        volume_path      TEXT,
        compute_type     TEXT,
        warehouse_id     TEXT,
        bootstrapped_at  DOUBLE PRECISION,
        download_job_id  BIGINT
    );

    CREATE TABLE IF NOT EXISTS watermarks (
        user_id    TEXT NOT NULL,
        project_id TEXT NOT NULL,
        data_type  TEXT NOT NULL,
        last_sync  DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (user_id, project_id, data_type)
    );

    CREATE TABLE IF NOT EXISTS sync_runs (
        run_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        user_id         TEXT NOT NULL,
        project_id      TEXT NOT NULL,
        data_types      TEXT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'pending',
        record_counts   TEXT,
        skipped_modules TEXT,
        bronze_run_id   BIGINT,
        silver_run_id   BIGINT,
        error           TEXT,
        started_at      DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL,
        user_run_seq    BIGINT
    );

    CREATE TABLE IF NOT EXISTS m2m_config (
        config_id        BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        workspace_url    TEXT NOT NULL,
        acc_account_id   TEXT NOT NULL,
        hub_id           TEXT NOT NULL,
        hub_name         TEXT,
        project_id       TEXT NOT NULL,
        project_name     TEXT,
        catalog_name     TEXT NOT NULL,
        validated_at     DOUBLE PRECISION,
        saved_at         DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS m2m_bootstrap_state (
        config_id          BIGINT PRIMARY KEY,
        bronze_job_id      BIGINT,
        silver_job_id      BIGINT,
        notebook_folder    TEXT,
        volume_path        TEXT,
        warehouse_id       TEXT,
        catalog_name       TEXT,
        bronze_pipeline_id TEXT,
        cdc_pipeline_id    TEXT,
        bootstrapped_at    DOUBLE PRECISION
    );

    CREATE TABLE IF NOT EXISTS m2m_sync_runs (
        run_id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        config_id       BIGINT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'pending',
        record_counts   TEXT,
        bronze_run_id   BIGINT,
        silver_run_id   BIGINT,
        error           TEXT,
        started_at      DOUBLE PRECISION NOT NULL,
        updated_at      DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS m2m_watermarks (
        config_id  BIGINT NOT NULL,
        data_type  TEXT NOT NULL DEFAULT 'data_connector',
        last_sync  DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (config_id, data_type)
    );

    CREATE TABLE IF NOT EXISTS app_secrets (
        user_id                  TEXT PRIMARY KEY,
        DATABRICKS_CLIENT_ID     TEXT,
        DATABRICKS_CLIENT_SECRET TEXT,
        DATABRICKS_WORKSPACE_URL TEXT,
        updated_at               DOUBLE PRECISION NOT NULL
    );
''' + (_OK_SCHEMA_BASE + _M2M_SCHEMA_BASE) \
        .replace('%ID%', 'BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY') \
        .replace('%INT%', 'BIGINT') \
        .replace('%REAL%', 'DOUBLE PRECISION')

_MIGRATIONS = [
    'ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS skipped_modules TEXT',
    'ALTER TABLE acc_config ADD COLUMN IF NOT EXISTS acc_account_id TEXT',
    "ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS trigger_type TEXT DEFAULT 'manual'",
    "ALTER TABLE dbx_tokens ADD COLUMN IF NOT EXISTS cloud_provider TEXT NOT NULL DEFAULT 'unknown'",
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS catalog_name TEXT',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS bronze_pipeline_id TEXT',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS snapshot_pipeline_id TEXT',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS download_job_id INTEGER',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS cdc_pipeline_id TEXT',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS snapshot_workflow_id INTEGER',
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS cdc_workflow_id INTEGER',
    'ALTER TABLE acc_config ADD COLUMN IF NOT EXISTS daily_cdc_enabled INTEGER NOT NULL DEFAULT 0',
    'ALTER TABLE acc_config ADD COLUMN IF NOT EXISTS auto_cdc_next_run_at REAL',
    'ALTER TABLE m2m_bootstrap_state ADD COLUMN IF NOT EXISTS snapshot_pipeline_id TEXT',
    'ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS user_run_seq INTEGER',
    # catalog_claim_id links a user's bootstrap to the catalog claim that
    # authorises it. NULL on rows predating per-catalog pipelines until
    # _backfill_catalog_claims fills them in.
    'ALTER TABLE bootstrap_state ADD COLUMN IF NOT EXISTS catalog_claim_id INTEGER',
    # Phase 1 — orphan adoption for SSA provisioning (see tenants DDL above).
    'ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pending_service_account_id TEXT',
    'ALTER TABLE tenants ADD COLUMN IF NOT EXISTS pending_robot_email TEXT',
    'ALTER TABLE tenants ADD COLUMN IF NOT EXISTS provisioning_claimed_at REAL',
]


def _backfill_user_run_seq(con) -> None:
    """Assign per-user sequence numbers to rows created before user_run_seq existed."""
    rows = con.execute('''
        SELECT run_id, user_id FROM sync_runs
        WHERE user_run_seq IS NULL
        ORDER BY user_id, run_id
    ''').fetchall()
    if not rows:
        return
    seq_by_user: dict[str, int] = {}
    for row in rows:
        uid = row['user_id']
        seq_by_user[uid] = seq_by_user.get(uid, 0) + 1
        con.execute(
            'UPDATE sync_runs SET user_run_seq = ? WHERE run_id = ?',
            (seq_by_user[uid], row['run_id']),
        )

def _backfill_catalog_claims(con) -> None:
    """Give every pre-existing bootstrap a catalog claim owned by its user.

    Without this, the first user to re-bootstrap after the per-catalog change
    could claim a catalog another user had already provisioned.

    Idempotent: rows that already have an active claim are skipped. Users with
    no ``dbx_tokens`` row are skipped too — without a workspace URL the claim
    cannot be scoped, and an unscoped claim would lock the catalog name across
    every workspace.
    """
    # Deferred imports: this migration runs once at startup, and importing at
    # module scope would create a cycle (the repository imports _conn from here).
    from .catalog_claim_repository import normalize_workspace_key
    from backend.services.pipeline_naming import artifact_names

    try:
        rows = con.execute('''
            SELECT b.user_id, b.catalog_name, b.snapshot_pipeline_id,
                   b.cdc_pipeline_id, b.snapshot_workflow_id, b.cdc_workflow_id,
                   t.workspace_url
            FROM bootstrap_state b
            LEFT JOIN dbx_tokens t ON t.user_id = b.user_id
            WHERE b.catalog_name IS NOT NULL AND TRIM(b.catalog_name) != ''
              AND b.catalog_claim_id IS NULL
        ''').fetchall()
    except Exception:
        return

    now = time.time()
    for row in rows:
        workspace_key = normalize_workspace_key(row['workspace_url'])
        if not workspace_key:
            continue
        catalog_name = row['catalog_name'].strip()
        names = artifact_names(catalog_name)
        existing = con.execute('''
            SELECT claim_id, owner_user_id FROM catalog_claims
            WHERE workspace_key = ? AND catalog_name = ? AND released_at IS NULL
        ''', (workspace_key, catalog_name)).fetchone()
        if existing:
            if existing['owner_user_id'] == row['user_id']:
                con.execute(
                    'UPDATE bootstrap_state SET catalog_claim_id = ? WHERE user_id = ?',
                    (existing['claim_id'], row['user_id']),
                )
            continue
        cur = con.execute('''
            INSERT INTO catalog_claims
                (workspace_key, catalog_name, owner_user_id,
                 snapshot_pipeline_id, cdc_pipeline_id,
                 snapshot_pipeline_name, cdc_pipeline_name,
                 snapshot_workflow_id, cdc_workflow_id,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING claim_id
        ''', (workspace_key, catalog_name, row['user_id'],
              row['snapshot_pipeline_id'], row['cdc_pipeline_id'],
              names['snapshot_pipeline'], names['cdc_pipeline'],
              row['snapshot_workflow_id'], row['cdc_workflow_id'], now))
        row_id = cur.fetchone()['claim_id']
        con.execute(
            'UPDATE bootstrap_state SET catalog_claim_id = ? WHERE user_id = ?',
            (row_id, row['user_id']),
        )


def _prepare_app_secrets(con) -> None:
    """Recreate app_secrets if an old-format table already exists.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a table
    created by older code keeps whatever shape it had. Drop it unless it already
    matches the current wide ``user_id``-keyed Databricks-only shape, letting
    the schema below create the correct one fresh. Safe no-op when absent or
    already correct.
    """
    try:
        rows = con.execute("""
            SELECT column_name AS c
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'app_secrets'
        """).fetchall()
    except Exception:
        rows = [r[1] for r in con.execute('PRAGMA table_info(app_secrets)').fetchall()]
    if not rows:
        return
    cols = [r['c'] if isinstance(r, dict) else r for r in rows]
    target = {
        'user_id',
        'databricks_client_id',
        'databricks_client_secret',
        'databricks_workspace_url',
    }
    if not target.issubset(c.lower() for c in cols):
        con.execute('DROP TABLE IF EXISTS app_secrets')


def _seed_default_aps_app(con) -> None:
    """Register the vendor APS app as shard ``app_a`` on first start (FR-05 §5.1, §13).

    Only ever inserts. A Client ID rotated directly in ``aps_apps`` must survive a restart
    that still has the old value in ``.env``, and ``tenants.aps_app_ref`` is immutable, so
    an UPDATE here could silently point live hubs at the wrong credentials.

    Prefers ``APS_SSA_CLIENT_ID`` over ``APS_CLIENT_ID``: creating service accounts requires a
    **Server-to-Server** app, while the 3LO sign-in in ``auth_client`` requires an app with a
    callback URL. Autodesk will not let one app be both, so the two roles need two Client IDs.
    The fallback keeps single-app deployments that predate the split working unchanged.
    """
    client_id = (
        os.getenv('APS_SSA_CLIENT_ID') or os.getenv('APS_CLIENT_ID') or ''
    ).strip()
    if not client_id:
        return
    try:
        existing = con.execute(
            'SELECT 1 FROM aps_apps WHERE app_ref = ?', ('app_a',)
        ).fetchone()
        if existing:
            return
        # Deferred import: backend.secrets sits below repositories, and importing it at
        # module scope would drag the secret backends into every repository import.
        from backend.secrets import aps_app_secret_ref

        con.execute(
            'INSERT INTO aps_apps (app_ref, client_id, client_secret_ref, max_robots,'
            ' display_name, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                'app_a',
                client_id,
                aps_app_secret_ref('app_a'),
                10,
                'Primary APS App',
                1,
                time.time(),
            ),
        )
    except Exception:
        # A duplicate client_id from a concurrent start, or a partially migrated DB,
        # must not stop the app booting — provisioning surfaces the problem instead.
        pass


def init_db() -> None:
    """Create the database (Postgres) and all tables if they do not exist.
    Safe to call on every startup."""
    _ensure_database()
    schema = _PG_SCHEMA if _is_pg() else _SQLITE_SCHEMA
    with _conn() as con:
        _prepare_app_secrets(con)
        con.executescript(schema)
        migrations = _MIGRATIONS if _is_pg() else [
            m.replace('ADD COLUMN IF NOT EXISTS', 'ADD COLUMN') for m in _MIGRATIONS
        ]
        for migration in migrations:
            try:
                con.execute(migration)
            except Exception:
                pass
        _backfill_user_run_seq(con)
        _backfill_catalog_claims(con)
        _seed_default_aps_app(con)
        try:
            from .secret_repository import encrypt_existing_plaintext
            encrypt_existing_plaintext()
        except Exception:
            pass
        for table in ('bootstrap_state', 'm2m_bootstrap_state'):
            try:
                con.execute(f'''
                    UPDATE {table}
                    SET snapshot_pipeline_id = bronze_pipeline_id
                    WHERE snapshot_pipeline_id IS NULL
                      AND bronze_pipeline_id IS NOT NULL
                ''')
            except Exception:
                pass
