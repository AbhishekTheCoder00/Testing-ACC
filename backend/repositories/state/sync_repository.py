
import json
import time
from .database import _conn

SYNC_STALE_SECONDS = 2 * 3600

# ---------------------------------------------------------------------------
# Sync runs
# ---------------------------------------------------------------------------

def create_sync_run(user_id: str, project_id: str, data_types: list,
                    trigger_type: str = 'manual') -> int:
    now = time.time()
    with _conn() as con:
        row = con.execute(
            'SELECT COALESCE(MAX(user_run_seq), 0) + 1 AS n FROM sync_runs WHERE user_id = ?',
            (user_id,),
        ).fetchone()
        user_run_seq = int(row['n'])
        cur = con.execute('''
            INSERT INTO sync_runs
                (user_id, project_id, data_types, state, trigger_type,
                 user_run_seq, started_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
            RETURNING run_id
        ''', (user_id, project_id, ','.join(data_types), trigger_type,
              user_run_seq, now, now))
        return cur.fetchone()['run_id']


def update_sync_run(run_id: int, state: str, **kwargs) -> None:
    """Update sync run state and any optional fields (record_counts, bronze_run_id, error)."""
    fields = {'state': state, 'updated_at': time.time()}
    fields.update(kwargs)
    cols = ', '.join(f'{k} = ?' for k in fields)
    vals = list(fields.values()) + [run_id]
    with _conn() as con:
        con.execute(f'UPDATE sync_runs SET {cols} WHERE run_id = ?', vals)


def get_sync_run(run_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM sync_runs WHERE run_id = ?', (run_id,),
        ).fetchone()
    return dict(row) if row else None


def get_latest_sync_run(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('''
            SELECT * FROM sync_runs WHERE user_id = ?
            ORDER BY run_id DESC LIMIT 1
        ''', (user_id,)).fetchone()
    return dict(row) if row else None


def _sync_run_mode(run: dict) -> str:
    """Return ``snapshot`` or ``cdc`` for a sync_runs row."""
    data_types = run.get('data_types') or ''
    if 'data_connector_cdc' in data_types:
        return 'cdc'
    raw = run.get('record_counts')
    if raw:
        try:
            rc = json.loads(raw)
            mode = rc.get('mode')
            if mode in ('snapshot', 'cdc'):
                return mode
        except (TypeError, json.JSONDecodeError):
            pass
    return 'snapshot'


def get_latest_snapshot_run(user_id: str, project_id: str) -> dict | None:
    """Most recent snapshot sync run for a project."""
    with _conn() as con:
        rows = con.execute('''
            SELECT * FROM sync_runs
            WHERE user_id = ? AND project_id = ?
            ORDER BY run_id DESC LIMIT 40
        ''', (user_id, project_id)).fetchall()
    for row in rows:
        run = dict(row)
        if _sync_run_mode(run) == 'snapshot':
            return run
    return None


def get_latest_cdc_run(user_id: str, project_id: str) -> dict | None:
    """Most recent CDC sync run (auto or manual) for a project."""
    with _conn() as con:
        rows = con.execute('''
            SELECT * FROM sync_runs
            WHERE user_id = ? AND project_id = ?
            ORDER BY run_id DESC LIMIT 40
        ''', (user_id, project_id)).fetchall()
    for row in rows:
        run = dict(row)
        if _sync_run_mode(run) == 'cdc':
            return run
    return None


def get_sync_history(user_id: str, trigger_type: str = None, limit: int = 10) -> list:
    """Return the last N sync runs, optionally filtered by trigger_type."""
    with _conn() as con:
        if trigger_type:
            rows = con.execute('''
                SELECT * FROM sync_runs
                WHERE user_id = ? AND trigger_type = ?
                ORDER BY run_id DESC LIMIT ?
            ''', (user_id, trigger_type, limit)).fetchall()
        else:
            rows = con.execute('''
                SELECT * FROM sync_runs
                WHERE user_id = ?
                ORDER BY run_id DESC LIMIT ?
            ''', (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_latest_sync_for_project(user_id: str, project_id: str,
                                 trigger_type: str = None) -> dict | None:
    """Return the most recent sync run for a specific project."""
    with _conn() as con:
        if trigger_type:
            row = con.execute('''
                SELECT * FROM sync_runs
                WHERE user_id = ? AND project_id = ? AND trigger_type = ?
                ORDER BY run_id DESC LIMIT 1
            ''', (user_id, project_id, trigger_type)).fetchone()
        else:
            row = con.execute('''
                SELECT * FROM sync_runs
                WHERE user_id = ? AND project_id = ?
                ORDER BY run_id DESC LIMIT 1
            ''', (user_id, project_id)).fetchone()
    return dict(row) if row else None


def get_last_successful_sync_timestamp(
    user_id: str,
    project_id: str,
    watermark_key: str,
) -> float | None:
    """``updated_at`` of the newest complete sync run that advanced ``watermark_key``."""
    from backend.services.sync.watermark_service import pick_successful_sync_timestamp

    with _conn() as con:
        rows = con.execute('''
            SELECT state, updated_at, record_counts, data_types FROM sync_runs
            WHERE user_id = ? AND project_id = ? AND state = 'complete'
            ORDER BY run_id DESC LIMIT 50
        ''', (user_id, project_id)).fetchall()
    return pick_successful_sync_timestamp([dict(r) for r in rows], watermark_key)


def has_successful_snapshot(user_id: str, project_id: str) -> bool:
    """True after a completed Sync Snapshot (Pipeline A + B baseline)."""
    with _conn() as con:
        rows = con.execute('''
            SELECT record_counts FROM sync_runs
            WHERE user_id = ? AND project_id = ? AND state = 'complete'
            ORDER BY run_id DESC LIMIT 30
        ''', (user_id, project_id)).fetchall()
    for row in rows:
        raw = row['record_counts']
        if not raw:
            continue
        try:
            rc = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if rc.get('mode') == 'snapshot':
            return True
    return False


def has_in_flight_sync_run(user_id: str) -> dict | None:
    """Mark abandoned runs failed; return the newest non-terminal run if any."""
    cutoff = time.time() - SYNC_STALE_SECONDS
    with _conn() as con:
        stale = con.execute('''
            SELECT run_id FROM sync_runs
            WHERE user_id = ? AND state NOT IN ('complete', 'failed', 'partial')
              AND updated_at < ?
        ''', (user_id, cutoff)).fetchall()
        for r in stale:
            con.execute('''
                UPDATE sync_runs
                SET state = 'failed',
                    error = COALESCE(error, 'Sync abandoned (process exited mid-run)'),
                    updated_at = ?
                WHERE run_id = ?
            ''', (time.time(), r['run_id']))

        row = con.execute('''
            SELECT * FROM sync_runs
            WHERE user_id = ? AND state NOT IN ('complete', 'failed', 'partial')
            ORDER BY run_id DESC LIMIT 1
        ''', (user_id,)).fetchone()
    return dict(row) if row else None

