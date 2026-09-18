"""
Purpose: APS imposes a quota of 10 service accounts per Client ID, so the ISV runs several
Server-to-Server apps ("shards") and assigns each hub to one. This repository owns the
`aps_apps` registry those shards live in. Ordering in list_active is deterministic
(app_ref ascending) because shard selection must fill app_a before app_b. See FR-04 §4.1.
"""

from __future__ import annotations

import time

from .database import _conn

DEFAULT_MAX_ROBOTS = 10


def insert(
    app_ref: str,
    client_id: str,
    client_secret_ref: str,
    *,
    max_robots: int = DEFAULT_MAX_ROBOTS,
    display_name: str | None = None,
) -> dict:
    """Register an APS app shard. The client secret itself lives in the SecretStore."""
    with _conn() as con:
        con.execute(
            'INSERT INTO aps_apps (app_ref, client_id, client_secret_ref, max_robots,'
            ' display_name, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (app_ref, client_id, client_secret_ref, int(max_robots), display_name,
             1, time.time()),
        )
    return get(app_ref)


def get(app_ref: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM aps_apps WHERE app_ref = ?', (app_ref,),
        ).fetchone()
    return _row(row)


def get_by_client_id(client_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM aps_apps WHERE client_id = ?', (client_id,),
        ).fetchone()
    return _row(row)


def list_active() -> list[dict]:
    """Active shards, lowest app_ref first — shard selection depends on this order."""
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM aps_apps WHERE is_active = 1 ORDER BY app_ref',
        ).fetchall()
    return [_row(r) for r in rows]


def set_active(app_ref: str, active: bool) -> None:
    """Take a shard out of rotation. Existing hubs on it keep working — only new
    provisioning skips it, because tenants.aps_app_ref is immutable."""
    with _conn() as con:
        con.execute(
            'UPDATE aps_apps SET is_active = ? WHERE app_ref = ?',
            (1 if active else 0, app_ref),
        )


def _row(row) -> dict | None:
    if not row:
        return None
    out = dict(row)
    out['is_active'] = bool(out.get('is_active'))
    out['max_robots'] = int(out.get('max_robots') or DEFAULT_MAX_ROBOTS)
    return out
