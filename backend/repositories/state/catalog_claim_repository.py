"""catalog_claim_repository.py — who owns which Unity Catalog.

One active claim per (workspace, catalog); one active claim per user. A claim
is what authorises a user to provision and sync a catalog, and it is the record
the UI consults to decide whether a catalog is available, owned, or locked.

Claiming is atomic via the partial unique index on
``(workspace_key, catalog_name) WHERE released_at IS NULL`` — concurrent
bootstraps race on INSERT and exactly one wins.
"""
import sqlite3
import time
from urllib.parse import urlparse

from .database import _conn


class CatalogLockedError(RuntimeError):
    """A catalog is already claimed by a different user."""

    def __init__(self, catalog_name: str):
        super().__init__(
            f'Catalog "{catalog_name}" is already in use by another user.'
        )
        self.catalog_name = catalog_name


def normalize_workspace_key(workspace_url: str | None) -> str:
    """Reduce a workspace URL to a stable host key.

    Catalog names are only unique within a workspace, so every claim is scoped
    by host. Users paste workspace URLs in many shapes (trailing slash, no
    scheme, ``/browse?o=...``); all of them must map to the same key or the
    same catalog would be claimable twice.
    """
    raw = (workspace_url or '').strip().rstrip('/')
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    return (urlparse(raw).netloc or '').strip().lower()


def get_active_claim(workspace_key: str, catalog_name: str) -> dict | None:
    """Return the active claim on this catalog, or None if unclaimed."""
    if not workspace_key or not catalog_name:
        return None
    with _conn() as con:
        row = con.execute('''
            SELECT * FROM catalog_claims
            WHERE workspace_key = ? AND catalog_name = ? AND released_at IS NULL
        ''', (workspace_key, catalog_name)).fetchone()
    return dict(row) if row else None


def get_user_claim(workspace_key: str, owner_user_id: str) -> dict | None:
    """Return this user's active claim in this workspace, or None."""
    if not workspace_key or not owner_user_id:
        return None
    with _conn() as con:
        row = con.execute('''
            SELECT * FROM catalog_claims
            WHERE workspace_key = ? AND owner_user_id = ? AND released_at IS NULL
            ORDER BY created_at DESC LIMIT 1
        ''', (workspace_key, owner_user_id)).fetchone()
    return dict(row) if row else None


def get_claim(claim_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM catalog_claims WHERE claim_id = ?',
            (claim_id,),
        ).fetchone()
    return dict(row) if row else None


def list_active_claims(workspace_key: str) -> list[dict]:
    """Every active claim in one workspace — used to annotate the catalog list."""
    if not workspace_key:
        return []
    with _conn() as con:
        rows = con.execute('''
            SELECT * FROM catalog_claims
            WHERE workspace_key = ? AND released_at IS NULL
        ''', (workspace_key,)).fetchall()
    return [dict(r) for r in rows]


def claim_catalog(workspace_key: str, catalog_name: str,
                  owner_user_id: str) -> dict:
    """Claim a catalog for a user. Idempotent for the existing owner.

    Raises ``CatalogLockedError`` if someone else holds the claim. Claiming a
    second catalog releases the user's previous claim — a user provisions one
    catalog at a time.
    """
    if not workspace_key:
        raise ValueError('workspace_key is required to claim a catalog')
    if not catalog_name:
        raise ValueError('catalog_name is required to claim a catalog')
    if not owner_user_id:
        raise ValueError('owner_user_id is required to claim a catalog')

    now = time.time()
    with _conn() as con:
        existing = con.execute('''
            SELECT * FROM catalog_claims
            WHERE workspace_key = ? AND catalog_name = ? AND released_at IS NULL
        ''', (workspace_key, catalog_name)).fetchone()
        if existing:
            if existing['owner_user_id'] != owner_user_id:
                raise CatalogLockedError(catalog_name)
            return dict(existing)

        # One catalog per user: drop any other claim this user still holds.
        con.execute('''
            UPDATE catalog_claims SET released_at = ?
            WHERE workspace_key = ? AND owner_user_id = ? AND released_at IS NULL
        ''', (now, workspace_key, owner_user_id))

        try:
            cur = con.execute('''
                INSERT INTO catalog_claims
                    (workspace_key, catalog_name, owner_user_id, created_at)
                VALUES (?, ?, ?, ?)
            ''', (workspace_key, catalog_name, owner_user_id, now))
        except sqlite3.IntegrityError:
            # Lost the race against a concurrent claim on the same catalog.
            raise CatalogLockedError(catalog_name)
        claim_id = cur.lastrowid
        row = con.execute(
            'SELECT * FROM catalog_claims WHERE claim_id = ?',
            (claim_id,),
        ).fetchone()
    return dict(row)


def release_claim(claim_id: int) -> bool:
    """Release one claim. Returns False if it was already released/absent."""
    with _conn() as con:
        cur = con.execute('''
            UPDATE catalog_claims SET released_at = ?
            WHERE claim_id = ? AND released_at IS NULL
        ''', (time.time(), claim_id))
    return cur.rowcount > 0


def release_user_claims(owner_user_id: str) -> int:
    """Release every active claim held by a user, across all workspaces."""
    with _conn() as con:
        cur = con.execute('''
            UPDATE catalog_claims SET released_at = ?
            WHERE owner_user_id = ? AND released_at IS NULL
        ''', (time.time(), owner_user_id))
    return cur.rowcount


_ARTIFACT_COLUMNS = (
    'snapshot_pipeline_id',
    'cdc_pipeline_id',
    'snapshot_pipeline_name',
    'cdc_pipeline_name',
    'snapshot_workflow_id',
    'cdc_workflow_id',
)


def update_claim_artifacts(claim_id: int, **fields) -> None:
    """Record the artifacts provisioned for a claim.

    Only columns in ``_ARTIFACT_COLUMNS`` are writable — ownership and
    workspace scoping are set at claim time and never updated.
    """
    updates = {k: v for k, v in fields.items() if k in _ARTIFACT_COLUMNS}
    unknown = set(fields) - set(_ARTIFACT_COLUMNS)
    if unknown:
        raise ValueError(f'Not an updatable claim column: {sorted(unknown)}')
    if not updates:
        return
    assignments = ', '.join(f'{col} = ?' for col in updates)
    with _conn() as con:
        con.execute(
            f'UPDATE catalog_claims SET {assignments} WHERE claim_id = ?',
            (*updates.values(), claim_id),
        )


def owns_catalog(workspace_key: str, catalog_name: str, user_id: str) -> bool:
    """True unless another user holds the claim.

    An unclaimed catalog returns True: legacy deployments whose claim could not
    be backfilled (no stored workspace URL) must keep working rather than lock
    their owner out.
    """
    claim = get_active_claim(workspace_key, catalog_name)
    if claim is None:
        return True
    return claim['owner_user_id'] == user_id
