"""
Purpose: A connection is one installed pipeline — (hub, ACC project, Databricks workspace,
catalog) — and owns its pipeline ids, Databricks SP credential ref, bootstrap state and
schedule (FR-02 FR-23, FR-05 §5.5-5.7). This repository is the only writer of those three
tables. Reads are hub- or connection-scoped by signature so a route cannot leak another
tenant's rows by forgetting a filter.
"""

from __future__ import annotations

import time

from .database import _conn

# FR-05 §8. `pending_ssa` (not FR-02's `pending_ssa_provisioned`) — the DB spec owns the
# stored vocabulary and the UI maps it to a label; see ADR.md (Phase 1).
CONNECTION_STATUSES = (
    'pending_custom_integration',
    'pending_ssa',
    'pending_databricks',
    'pending_bootstrap',
    'ready',
)

_BOOTSTRAP_COLUMNS = (
    'bronze_job_id',
    'silver_job_id',
    'notebook_folder',
    'volume_path',
    'warehouse_id',
    'catalog_name',
    'snapshot_workflow_id',
    'cdc_workflow_id',
    'download_job_id',
    'catalog_claim_id',
)


def _normalize_workspace(workspace_url: str) -> str:
    return (workspace_url or '').strip().rstrip('/')


def _row(row) -> dict | None:
    if not row:
        return None
    out = dict(row)
    if 'cdc_enabled' in out:
        out['cdc_enabled'] = bool(out['cdc_enabled'])
    return out


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------


def insert(
    *,
    connection_id: str,
    hub_id: str,
    project_id: str,
    dbx_workspace_url: str,
    catalog: str,
    project_name: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Create a connection. Raises on the UNIQUE quadruple — callers treat that as
    "resume the existing one" rather than an error (TC-CONN-02, TC-CONC-03)."""
    with _conn() as con:
        con.execute(
            'INSERT INTO connections (connection_id, tenant_id, hub_id, project_id,'
            ' project_name, dbx_workspace_url, catalog, onboarding_status, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (connection_id, tenant_id or hub_id, hub_id, project_id, project_name,
             _normalize_workspace(dbx_workspace_url), catalog,
             'pending_custom_integration', time.time()),
        )
    return get(connection_id)


def get(connection_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connections WHERE connection_id = ?', (connection_id,),
        ).fetchone()
    return _row(row)


def get_by_parts(hub_id: str, project_id: str, workspace_url: str, catalog: str) -> dict | None:
    """Look up by the natural key. The workspace URL is normalised so a trailing slash
    does not hide an existing connection and cause a duplicate bootstrap."""
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connections WHERE hub_id = ? AND project_id = ?'
            ' AND dbx_workspace_url = ? AND catalog = ?',
            (hub_id, project_id, _normalize_workspace(workspace_url), catalog),
        ).fetchone()
    return _row(row)


def list_for_hub(hub_id: str) -> list[dict]:
    """Every connection for one hub — shared by all of that hub's admins (FR-02 FR-21)."""
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM connections WHERE hub_id = ? ORDER BY created_at', (hub_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def update_status(connection_id: str, status: str, *, last_error: str | None = None) -> None:
    if status not in CONNECTION_STATUSES:
        raise ValueError(f'unknown connection onboarding_status: {status!r}')
    with _conn() as con:
        con.execute(
            'UPDATE connections SET onboarding_status = ?, last_error = ?'
            ' WHERE connection_id = ?',
            (status, last_error, connection_id),
        )


def backfill_project_name(connection_id: str, project_name: str) -> None:
    """Fill in a missing project name only. Never overwrites: the name shown in the UI
    should not flip because one caller passed a stale label (same rule as tenants)."""
    with _conn() as con:
        con.execute(
            'UPDATE connections SET project_name = ?'
            " WHERE connection_id = ? AND (project_name IS NULL OR project_name = '')",
            (project_name, connection_id),
        )


def set_pipeline_ids(
    connection_id: str,
    *,
    snapshot_pipeline_id: str | None = None,
    cdc_pipeline_id: str | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            'UPDATE connections SET snapshot_pipeline_id = ?, cdc_pipeline_id = ?'
            ' WHERE connection_id = ?',
            (snapshot_pipeline_id, cdc_pipeline_id, connection_id),
        )


def set_schedule(
    connection_id: str,
    *,
    enabled: bool,
    next_run_at: float | None = None,
) -> None:
    """Toggle headless CDC. Callers must have checked the connection has SP credentials —
    see has_dbx_credentials; a scheduled run may never fall back to a human token (BR-08)."""
    with _conn() as con:
        con.execute(
            'UPDATE connections SET cdc_enabled = ?, auto_cdc_next_run_at = ?'
            ' WHERE connection_id = ?',
            (1 if enabled else 0, next_run_at, connection_id),
        )


def list_due_for_cdc(now: float | None = None) -> list[dict]:
    """Connections the scheduler may run right now.

    Requires ready + cdc_enabled + a stored service principal. The credential join is the
    D-4 guard: a connection authorised with a human's Databricks OAuth token must never be
    picked up by a cron tick. A NULL next_run_at means "due immediately".
    """
    cutoff = time.time() if now is None else now
    with _conn() as con:
        rows = con.execute(
            'SELECT c.* FROM connections c'
            ' JOIN connection_dbx_credentials d ON d.connection_id = c.connection_id'
            " WHERE c.onboarding_status = 'ready' AND c.cdc_enabled = 1"
            ' AND (c.auto_cdc_next_run_at IS NULL OR c.auto_cdc_next_run_at <= ?)'
            ' ORDER BY c.auto_cdc_next_run_at',
            (cutoff,),
        ).fetchall()
    return [_row(r) for r in rows]


def delete(connection_id: str) -> None:
    """Remove a connection and its child rows (FR-05 §9.3). Explicit deletes because
    SQLite does not enforce ON DELETE CASCADE unless the foreign_keys pragma is on."""
    with _conn() as con:
        con.execute(
            'DELETE FROM connection_watermarks WHERE connection_id = ?', (connection_id,),
        )
        con.execute(
            'DELETE FROM connection_sync_runs WHERE connection_id = ?', (connection_id,),
        )
        con.execute(
            'DELETE FROM connection_bootstrap WHERE connection_id = ?', (connection_id,),
        )
        con.execute(
            'DELETE FROM connection_dbx_credentials WHERE connection_id = ?',
            (connection_id,),
        )
        con.execute('DELETE FROM connections WHERE connection_id = ?', (connection_id,))


# ---------------------------------------------------------------------------
# connection_dbx_credentials — tier T4, ref only
# ---------------------------------------------------------------------------


def save_dbx_credentials(
    connection_id: str,
    *,
    workspace_url: str,
    client_id: str,
    client_secret_ref: str,
    validated_at: float | None = None,
) -> None:
    with _conn() as con:
        con.execute(
            'INSERT INTO connection_dbx_credentials'
            ' (connection_id, workspace_url, client_id, client_secret_ref, validated_at)'
            ' VALUES (?, ?, ?, ?, ?)'
            ' ON CONFLICT(connection_id) DO UPDATE SET'
            '   workspace_url     = excluded.workspace_url,'
            '   client_id         = excluded.client_id,'
            '   client_secret_ref = excluded.client_secret_ref,'
            '   validated_at      = excluded.validated_at',
            (connection_id, _normalize_workspace(workspace_url), client_id,
             client_secret_ref, validated_at),
        )


def get_dbx_credentials(connection_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connection_dbx_credentials WHERE connection_id = ?',
            (connection_id,),
        ).fetchone()
    return dict(row) if row else None


def has_dbx_credentials(connection_id: str) -> bool:
    """Whether this connection can run headlessly at all (D-4)."""
    return get_dbx_credentials(connection_id) is not None


# ---------------------------------------------------------------------------
# connection_bootstrap
# ---------------------------------------------------------------------------


def save_bootstrap(connection_id: str, **fields) -> None:
    """Upsert the bootstrap result. Unknown keys are rejected rather than silently
    dropped — a typo here would otherwise lose a pipeline id."""
    unknown = set(fields) - set(_BOOTSTRAP_COLUMNS)
    if unknown:
        raise ValueError(f'unknown connection_bootstrap columns: {sorted(unknown)}')

    cols = list(_BOOTSTRAP_COLUMNS)
    values = [fields.get(c) for c in cols]
    placeholders = ', '.join('?' for _ in range(len(cols) + 2))
    updates = ', '.join(f'{c} = excluded.{c}' for c in cols)
    with _conn() as con:
        con.execute(
            f'INSERT INTO connection_bootstrap (connection_id, {", ".join(cols)},'
            f' bootstrapped_at) VALUES ({placeholders})'
            f' ON CONFLICT(connection_id) DO UPDATE SET {updates},'
            '  bootstrapped_at = excluded.bootstrapped_at',
            [connection_id, *values, time.time()],
        )


def get_bootstrap(connection_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connection_bootstrap WHERE connection_id = ?', (connection_id,),
        ).fetchone()
    return dict(row) if row else None
