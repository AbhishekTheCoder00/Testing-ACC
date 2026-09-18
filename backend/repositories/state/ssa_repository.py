"""
Purpose: One SSA robot per hub, and never a robot shared between hubs — the two invariants
the whole tenant-isolation model rests on (FR-02 BR-03, FR-04 TC-08). This repository owns
`ssa_credentials`, which stores robot metadata and a SecretStore *ref* only; the private key
PEM never reaches the database (FR-05 tier T3). Robot counts are derived here, not cached.
"""

from __future__ import annotations

import time

from .database import _conn


def get_by_hub_id(hub_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM ssa_credentials WHERE hub_id = ?', (hub_id,),
        ).fetchone()
    return dict(row) if row else None


def exists(hub_id: str) -> bool:
    """The CS-01 short circuit: if this is true, never call the APS create API again."""
    return get_by_hub_id(hub_id) is not None


def insert(
    *,
    hub_id: str,
    aps_app_ref: str,
    service_account_id: str,
    robot_email: str,
    key_id: str,
    private_key_ref: str,
    tenant_id: str | None = None,
) -> dict:
    """Persist a freshly provisioned robot.

    Raises on a duplicate hub_id or service_account_id — that is the point. The UNIQUE
    constraints are what make two admins clicking Provision at the same moment resolve to
    exactly one robot (FR-02 FR-28); callers catch the integrity error and re-read.
    """
    with _conn() as con:
        con.execute(
            'INSERT INTO ssa_credentials (tenant_id, hub_id, aps_app_ref,'
            ' service_account_id, robot_email, key_id, private_key_ref, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (tenant_id or hub_id, hub_id, aps_app_ref, service_account_id,
             robot_email, key_id, private_key_ref, time.time()),
        )
    return get_by_hub_id(hub_id)


def count_by_app_ref(app_ref: str) -> int:
    """Robots provisioned under one APS shard.

    Derived from the rows rather than a stored counter: the DB is the source of truth for
    *our* hubs (FR-05 §9.1), and a counter column would drift the first time a provision
    half-failed or a hub was offboarded.
    """
    with _conn() as con:
        row = con.execute(
            'SELECT COUNT(*) AS n FROM ssa_credentials WHERE aps_app_ref = ?', (app_ref,),
        ).fetchone()
    return int(row['n'])


def update_key(hub_id: str, *, key_id: str, private_key_ref: str) -> None:
    """Point the hub at a new signing key (FR-03 §12.3). The robot identity is unchanged —
    rotation must never create a second service account."""
    with _conn() as con:
        con.execute(
            'UPDATE ssa_credentials SET key_id = ?, private_key_ref = ?, rotated_at = ?'
            ' WHERE hub_id = ?',
            (key_id, private_key_ref, time.time(), hub_id),
        )


def delete(hub_id: str) -> None:
    """Drop the credential row so the shard slot frees up (FR-05 §9.3, FR-04 TC-09).
    The caller is responsible for deleting the vault secret and the APS robot itself."""
    with _conn() as con:
        con.execute('DELETE FROM ssa_credentials WHERE hub_id = ?', (hub_id,))
