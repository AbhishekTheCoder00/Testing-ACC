"""
Purpose: A tenant is a Forma/ACC hub — the isolation boundary for credentials, onboarding and
connections (FR-01). This repository owns `tenants` and the `tenant_users` hub-admin mapping.
ensure_tenant is insert-only for aps_app_ref and onboarding_status: the shard assignment is
immutable (FR-04 §4.2) and a second admin arriving must not reset the first admin's progress.
"""

from __future__ import annotations

import time

from .database import _conn

ROLE_HUB_ADMIN = 'hub_admin'

# FR-05 §8 plus the two failure states FR-06 §2.3 renders.
TENANT_STATUSES = (
    'pending_whitelist',
    'whitelist_verified',
    'ssa_active',
    'ssa_provision_failed',
    'ssa_limit_reached',
)


def get(hub_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM tenants WHERE hub_id = ?', (hub_id,)).fetchone()
    return dict(row) if row else None


def ensure_tenant(
    hub_id: str,
    aps_app_ref: str,
    *,
    acc_account_id: str | None = None,
    hub_name: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Return the hub's tenant, creating it on first sight.

    Deliberately does **not** update ``aps_app_ref`` or ``onboarding_status`` for an existing
    row. Reassigning the shard would point a live hub at credentials its robot was not created
    under (FR-04 §4.2), and resetting the status would undo the first admin's whitelist.
    Descriptive fields are filled in only where they are still empty.
    """
    existing = get(hub_id)
    if existing:
        _fill_blanks(hub_id, existing, acc_account_id=acc_account_id,
                     hub_name=hub_name, org_id=org_id)
        return get(hub_id)

    if not aps_app_ref:
        raise ValueError('aps_app_ref is required to create a tenant')

    try:
        with _conn() as con:
            con.execute(
                'INSERT INTO tenants (tenant_id, hub_id, aps_app_ref, acc_account_id, org_id,'
                ' hub_name, onboarding_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (hub_id, hub_id, aps_app_ref, acc_account_id, org_id, hub_name,
                 'pending_whitelist', time.time()),
            )
    except Exception:
        # Lost a race on UNIQUE(hub_id) — two admins onboarding the same hub at once.
        # The read above found nothing, so someone inserted in between; their row is as
        # good as ours. Re-raise only if the row genuinely is not there.
        if get(hub_id) is None:
            raise
    return get(hub_id)


def _fill_blanks(hub_id: str, existing: dict, **fields) -> None:
    updates = {k: v for k, v in fields.items() if v and not existing.get(k)}
    if not updates:
        return
    cols = ', '.join(f'{k} = ?' for k in updates)
    with _conn() as con:
        con.execute(
            f'UPDATE tenants SET {cols} WHERE hub_id = ?',
            [*updates.values(), hub_id],
        )


def update_status(hub_id: str, status: str, *, last_error: str | None = None) -> None:
    """Move the hub's onboarding status. Clears ``last_error`` unless a new one is given,
    so a recovered hub does not keep showing a stale failure banner."""
    if status not in TENANT_STATUSES:
        raise ValueError(f'unknown tenant onboarding_status: {status!r}')
    with _conn() as con:
        con.execute(
            'UPDATE tenants SET onboarding_status = ?, last_error = ? WHERE hub_id = ?',
            (status, last_error, hub_id),
        )


PROVISION_CLAIM_STALE_SEC = 300


def try_claim_provisioning(hub_id: str, stale_after: float = PROVISION_CLAIM_STALE_SEC) -> bool:
    """Atomically claim the right to create this hub's robot. True if we own it.

    A conditional UPDATE on one row is the mutex, so it holds across gunicorn workers where an
    in-process lock would not — two admins on two workers would otherwise each call
    ``POST /service-accounts`` and burn two of the ten slots on one hub (FR-02 FR-28).

    The claim expires so a worker that dies mid-provision does not lock the hub out forever;
    the next attempt takes over and adopts whatever robot the dead one left behind.
    """
    now = time.time()
    cutoff = now - stale_after
    with _conn() as con:
        cur = con.execute(
            'UPDATE tenants SET provisioning_claimed_at = ? WHERE hub_id = ?'
            ' AND (provisioning_claimed_at IS NULL OR provisioning_claimed_at < ?)',
            (now, hub_id, cutoff),
        )
        return (cur.rowcount or 0) > 0


def release_provisioning(hub_id: str) -> None:
    with _conn() as con:
        con.execute(
            'UPDATE tenants SET provisioning_claimed_at = NULL WHERE hub_id = ?', (hub_id,),
        )


def set_pending_robot(hub_id: str, service_account_id: str, robot_email: str) -> None:
    """Record a robot APS has created but whose key we have not stored yet.

    Written immediately after ``POST /service-accounts`` so that a failure in the vault write
    or the credential insert leaves an adoptable robot rather than an invisible one — the next
    attempt reuses it instead of consuming a second service-account slot for the same hub.
    """
    with _conn() as con:
        con.execute(
            'UPDATE tenants SET pending_service_account_id = ?, pending_robot_email = ?'
            ' WHERE hub_id = ?',
            (service_account_id, robot_email, hub_id),
        )


def clear_pending_robot(hub_id: str) -> None:
    """Drop the adoption marker once the credential row exists."""
    with _conn() as con:
        con.execute(
            'UPDATE tenants SET pending_service_account_id = NULL,'
            ' pending_robot_email = NULL WHERE hub_id = ?',
            (hub_id,),
        )


def set_ssa_verified(hub_id: str, ts: float | None = None) -> None:
    with _conn() as con:
        con.execute(
            'UPDATE tenants SET ssa_verified_at = ? WHERE hub_id = ?',
            (ts if ts is not None else time.time(), hub_id),
        )


# ---------------------------------------------------------------------------
# Hub-admin mapping
# ---------------------------------------------------------------------------


def add_user(hub_id: str, aps_user_id: str, role: str = ROLE_HUB_ADMIN) -> None:
    """Map a hub admin to a hub. Idempotent — a repeat login must not duplicate the row.

    Only ``hub_admin`` is storable: FR-02 FR-07 and BR-12 make this a hub-admin-only product,
    so accepting any other role here would be the first step to letting one in.
    """
    if role != ROLE_HUB_ADMIN:
        raise ValueError(
            f'only {ROLE_HUB_ADMIN!r} mappings are supported, got {role!r} '
            '(FR-02 FR-07: project admins and viewers are not product users)'
        )
    if is_member(hub_id, aps_user_id):
        return
    try:
        with _conn() as con:
            con.execute(
                'INSERT INTO tenant_users (tenant_id, aps_user_id, role, created_at)'
                ' VALUES (?, ?, ?, ?)',
                (hub_id, aps_user_id, role, time.time()),
            )
    except Exception:
        # Double OAuth callback racing on the composite PK (TC-CONC-05). Either way the
        # mapping now exists, which is all the caller wanted.
        if not is_member(hub_id, aps_user_id):
            raise


def is_member(hub_id: str, aps_user_id: str) -> bool:
    """The authorisation primitive every tenant-scoped route depends on."""
    with _conn() as con:
        row = con.execute(
            'SELECT 1 AS ok FROM tenant_users WHERE tenant_id = ? AND aps_user_id = ?',
            (hub_id, aps_user_id),
        ).fetchone()
    return row is not None


def list_users(hub_id: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            'SELECT * FROM tenant_users WHERE tenant_id = ? ORDER BY created_at',
            (hub_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_hubs_for_user(aps_user_id: str) -> list[dict]:
    """Tenants this admin is mapped to. Scoped by user on purpose — there is no
    unscoped list_tenants(), so a route cannot leak another hub by omission."""
    with _conn() as con:
        rows = con.execute(
            'SELECT t.* FROM tenants t'
            ' JOIN tenant_users u ON u.tenant_id = t.tenant_id'
            ' WHERE u.aps_user_id = ? ORDER BY t.hub_name, t.hub_id',
            (aps_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_tenant(hub_id: str) -> None:
    """Offboard a hub (FR-05 §9.3). Child rows are removed explicitly because SQLite does
    not enforce ON DELETE CASCADE unless foreign_keys pragma is on."""
    with _conn() as con:
        con.execute('DELETE FROM tenant_users WHERE tenant_id = ?', (hub_id,))
        con.execute('DELETE FROM ssa_credentials WHERE tenant_id = ?', (hub_id,))
        con.execute('DELETE FROM tenants WHERE hub_id = ?', (hub_id,))
