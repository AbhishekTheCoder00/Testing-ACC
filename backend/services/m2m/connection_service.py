"""
Purpose: Setup is re-entrant, so creating a connection has to be idempotent on the natural key
(hub, project, workspace, catalog) rather than minting a row per attempt (FR-02 FR-18…23).
This module owns that create-or-resume decision, the catalog exclusivity claim (BR-11, taken
through the existing catalog_claims table so U2M and M2M cannot both drive one catalog), the
onboarding status machine that drives wizard resume, and the D-4/D-18 gate on scheduled CDC.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from backend.repositories.state import (
    catalog_claim_repository as claims,
    connection_repository as conns,
    tenant_repository as tenants,
)
from backend.services.m2m import provisioning_probe
from backend.services.m2m.connection_identity import compute_connection_id

logger = logging.getLogger(__name__)

# Daily cadence for headless CDC (FR-03 §11.4). The U2M path has its own copy of this
# interval in routes/sync_routes.py; services must not import routes, and a shared constant
# module for one number is not worth the file.
CDC_INTERVAL_SEC = 24 * 3600

# FR-06 §4.8. next_step() maps a stored status onto one of these so a returning admin lands
# where they left off instead of at step one.
WIZARD_STEPS = ('whitelist', 'ssa', 'target', 'bootstrap', 'sync')

_STEP_FOR_STATUS = {
    'pending_custom_integration': 'whitelist',
    'pending_ssa': 'ssa',
    'pending_databricks': 'target',
    'pending_bootstrap': 'bootstrap',
    'ready': 'sync',
}

_CLAIM_OWNER_PREFIX = 'cnx:'


class CatalogAlreadyConnected(Exception):
    """The catalog is driven by another connection or by a U2M user."""

    def __init__(self, catalog: str, owner: str):
        super().__init__(
            f'Catalog "{catalog}" is already connected. Pick a different catalog, or '
            'disconnect the existing connection first.'
        )
        self.catalog = catalog
        self.owner = owner


class SchedulingBlocked(Exception):
    """Scheduled CDC was refused, and `remediation` says exactly what is missing.

    Never collapse these into a generic error: the whole point of D-18 is that the disabled
    toggle names the one action the customer has to take.
    """

    def __init__(self, reason: str, message: str, remediation: str = ''):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.remediation = remediation


@dataclass
class ConnectionResult:
    connection: dict
    resumed: bool


def claim_owner(connection_id: str) -> str:
    """The catalog_claims owner for a connection.

    Owned by the connection, not the admin who created it, so the claim survives that admin
    losing hub access (FR-02 FR-21) and is stable under the create race — both racers derive
    the same string from the same quadruple.
    """
    return f'{_CLAIM_OWNER_PREFIX}{connection_id}'


def claim_catalog(connection: dict) -> dict:
    """Take (or re-take) this connection's exclusive claim on its catalog.

    Idempotent for its own owner, so the bootstrap driver can call it again without checking.
    """
    workspace_key = claims.normalize_workspace_key(connection['dbx_workspace_url'])
    catalog = connection['catalog']
    try:
        return claims.claim_catalog(
            workspace_key, catalog, claim_owner(connection['connection_id']),
        )
    except claims.CatalogLockedError:
        holder = claims.get_active_claim(workspace_key, catalog) or {}
        raise CatalogAlreadyConnected(catalog, holder.get('owner_user_id', 'another user'))


def create_or_resume(
    *,
    hub_id: str,
    project_id: str,
    dbx_workspace_url: str,
    catalog: str,
    project_name: str | None = None,
) -> ConnectionResult:
    """Return this target's connection, creating it only if it does not exist yet.

    The catalog is claimed *before* the insert: a refused claim must leave no connection row
    behind for the wizard to resume into a catalog it cannot have.
    """
    connection_id = compute_connection_id(hub_id, project_id, dbx_workspace_url, catalog)

    existing = conns.get(connection_id)
    if existing:
        if project_name and not existing.get('project_name'):
            conns.backfill_project_name(connection_id, project_name)
            existing = conns.get(connection_id)
        claim_catalog(existing)
        return ConnectionResult(existing, resumed=True)

    pending = {
        'connection_id': connection_id,
        'dbx_workspace_url': dbx_workspace_url,
        'catalog': catalog,
    }
    claim_catalog(pending)

    try:
        row = conns.insert(
            connection_id=connection_id,
            hub_id=hub_id,
            project_id=project_id,
            project_name=project_name,
            dbx_workspace_url=dbx_workspace_url,
            catalog=catalog,
        )
    except sqlite3.IntegrityError:
        # Two admins on one hub picked the same target at the same time (TC-CONC-03). The
        # loser adopts the winner's row; the claim is already correct, both computed it.
        row = conns.get(connection_id)
        if not row:
            raise
        return ConnectionResult(row, resumed=True)

    return ConnectionResult(row, resumed=False)


def next_step(connection: dict) -> str:
    """Which wizard step this connection resumes at (TC-CONN-11)."""
    return _STEP_FOR_STATUS.get(connection.get('onboarding_status'), WIZARD_STEPS[0])


def mark_ready(connection_id: str) -> None:
    """Promote a bootstrapped connection to `ready` and activate its tenant.

    `ssa_active` means a connection actually works end to end, not merely that a robot exists
    (FR-03 §18 Q2) — and it only ever moves forward, so a hub sitting in a failure state is
    not silently marked healthy.
    """
    conns.update_status(connection_id, 'ready')
    connection = conns.get(connection_id)
    if not connection:
        return
    tenant = tenants.get(connection['hub_id'])
    if tenant and tenant['onboarding_status'] in ('pending_whitelist', 'whitelist_verified'):
        tenants.update_status(connection['hub_id'], 'ssa_active')


def enable_cdc(connection_id: str, *, now: float | None = None) -> dict:
    """Turn on headless daily CDC, but only once the whole chain is proven.

    Both halves are required (D-4 + D-18): a Databricks service principal, because a cron
    tick may never borrow a human's OAuth token, and a live robot-on-project probe, because
    whether the robot is still invited is knowable only by asking Autodesk (D-17).
    """
    connection = conns.get(connection_id)
    if not connection:
        raise SchedulingBlocked(
            'unknown_connection', 'This connection no longer exists.',
        )
    if connection['onboarding_status'] != 'ready':
        raise SchedulingBlocked(
            'not_bootstrapped',
            'This connection is not bootstrapped yet.',
            remediation='Finish setup for this connection, then enable scheduled sync.',
        )
    if not conns.has_dbx_credentials(connection_id):
        raise SchedulingBlocked(
            'missing_service_principal',
            'Scheduled sync needs a Databricks service principal.',
            remediation='Add a Databricks service principal to enable scheduled sync.',
        )

    probe = provisioning_probe.verify_robot_on_project(
        connection['hub_id'], connection['project_id'],
    )
    if not probe.passed:
        raise SchedulingBlocked(probe.code, probe.message, probe.remediation)

    next_run_at = (time.time() if now is None else now) + CDC_INTERVAL_SEC
    conns.set_schedule(connection_id, enabled=True, next_run_at=next_run_at)
    return conns.get(connection_id)


def disable_cdc(connection_id: str) -> dict | None:
    """Turn off headless CDC. Never probes — switching something off must always work."""
    conns.set_schedule(connection_id, enabled=False, next_run_at=None)
    return conns.get(connection_id)
