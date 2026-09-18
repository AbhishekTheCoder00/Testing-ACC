"""
Purpose: Run history and watermarks for one connection (FR-05 §5.8-5.9). Two behaviours here
are load bearing: has_in_flight_run implements the FR-05 §10.3 guard that stops a cron tick
overlapping a manual sync, and last_successful_timestamp anchors the next incremental window
on the last *successful* run so a failed retry cannot skip a window (FR-03 §18 Q7).
"""

from __future__ import annotations

import time

from .database import _conn

# FR-05 §10.3 — exactly these states mean "a run is still going".
IN_FLIGHT_STATES = frozenset({'pending', 'exporting', 'downloading', 'pipeline_running'})
TERMINAL_STATES = frozenset({'success', 'failed', 'cancelled'})
RUN_STATES = IN_FLIGHT_STATES | TERMINAL_STATES

MODES = frozenset({'snapshot', 'cdc'})


def create_run(connection_id: str, *, mode: str, trigger_type: str = 'manual') -> int:
    if mode not in MODES:
        raise ValueError(f'unknown sync mode: {mode!r}')
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            'INSERT INTO connection_sync_runs (connection_id, trigger_type, mode, state,'
            " started_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)"
            ' RETURNING run_id',
            (connection_id, trigger_type, mode, now, now),
        )
        return cur.fetchone()['run_id']


def update_run(run_id: int, state: str, **kwargs) -> None:
    """Advance a run. Optional columns: phase, record_counts, files_count,
    bronze_run_id, error."""
    if state not in RUN_STATES:
        raise ValueError(f'unknown sync run state: {state!r}')
    fields = {'state': state, 'updated_at': time.time()}
    fields.update(kwargs)
    cols = ', '.join(f'{k} = ?' for k in fields)
    with _conn() as con:
        con.execute(
            f'UPDATE connection_sync_runs SET {cols} WHERE run_id = ?',
            [*fields.values(), run_id],
        )


def get_run(run_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connection_sync_runs WHERE run_id = ?', (run_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_run(connection_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM connection_sync_runs WHERE connection_id = ?'
            ' ORDER BY run_id DESC LIMIT 1',
            (connection_id,),
        ).fetchone()
    return dict(row) if row else None


def list_runs(connection_id: str, limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM connection_sync_runs WHERE connection_id = ?'
            ' ORDER BY run_id DESC LIMIT ?',
            (connection_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def has_in_flight_run(connection_id: str) -> dict | None:
    """The concurrency guard (FR-03 §12.2, §18 Q7).

    Scoped to one connection on purpose: a hub may sync several connections in parallel,
    but a single connection must never have two overlapping runs.
    """
    placeholders = ', '.join('?' for _ in IN_FLIGHT_STATES)
    states = sorted(IN_FLIGHT_STATES)
    with _conn() as con:
        row = con.execute(
            f'SELECT * FROM connection_sync_runs WHERE connection_id = ?'
            f' AND state IN ({placeholders}) ORDER BY run_id DESC LIMIT 1',
            [connection_id, *states],
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------


def get_watermark(connection_id: str, data_type: str = 'data_connector') -> float | None:
    with _conn() as con:
        row = con.execute(
            'SELECT last_sync FROM connection_watermarks WHERE connection_id = ?'
            ' AND data_type = ?',
            (connection_id, data_type),
        ).fetchone()
    return float(row['last_sync']) if row else None


def set_watermark(
    connection_id: str,
    ts: float | None = None,
    data_type: str = 'data_connector',
) -> None:
    with _conn() as con:
        con.execute(
            'INSERT INTO connection_watermarks (connection_id, data_type, last_sync)'
            ' VALUES (?, ?, ?)'
            ' ON CONFLICT(connection_id, data_type) DO UPDATE SET'
            '   last_sync = excluded.last_sync',
            (connection_id, data_type, ts if ts is not None else time.time()),
        )


def last_successful_timestamp(connection_id: str, watermark_key: str) -> float | None:
    """When this connection last completed a run that advances ``watermark_key``.

    Anchoring on the last success rather than the last attempt is what stops a failed run
    from silently shrinking the next incremental window.
    """
    # Deferred import: mapping a watermark key to the run modes that advance it is sync
    # logic, not row storage. Same accepted exception as sync_repository — keep it local.
    from backend.services.sync.watermark_service import successful_sync_modes_for_watermark

    modes = sorted(successful_sync_modes_for_watermark(watermark_key))
    if not modes:
        return None
    placeholders = ', '.join('?' for _ in modes)
    with _conn() as con:
        row = con.execute(
            f'SELECT updated_at FROM connection_sync_runs WHERE connection_id = ?'
            f" AND state = 'success' AND mode IN ({placeholders})"
            f' ORDER BY run_id DESC LIMIT 1',
            [connection_id, *modes],
        ).fetchone()
    return float(row['updated_at']) if row else None
