"""
Purpose: Resolves which APS app a hub provisions its robot under, and guards the service-account
ceiling. v1 targets 10 enterprises and therefore runs a single Client ID at exactly the APS
default of 10 robots, so this is a capacity guard first and a shard selector second — the
selection loop exists because the registry supports more apps, not because v1 uses them.

Robot counts are derived with COUNT(ssa_credentials) rather than a stored counter: the database
is the source of truth for our hubs (FR-05 §9.1), and a counter would drift the first time a
provision half-failed or a hub was offboarded. See ADR.md (Phase 1).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from backend.repositories.state import (
    aps_app_repository,
    ssa_repository,
    tenant_repository,
)
from backend.services import ops_alert

logger = logging.getLogger(__name__)

QUOTA_ALERT_THRESHOLD = 0.8  # warn ops at 8/10


class ApsAppUnavailable(RuntimeError):
    """No APS app is registered — the deployment is not configured."""


@dataclass(frozen=True)
class PreCheckResult:
    status: str                      # already_provisioned | capacity_exhausted | ready
    client_id: str | None = None
    app_ref: str | None = None
    slots_remaining: int | None = None
    message: str = ''


def get_client_secret(app: dict) -> str:
    """The APS client secret for one app, from the SecretStore.

    Falls back to ``APS_SSA_CLIENT_SECRET``, then ``APS_CLIENT_SECRET``, when the store has no
    value at the ref, because ``init_db`` seeds the *reference* but only a deployment step can
    put the secret behind it. The fallback keeps local dev and the single-Client-ID v1 working
    from `.env`; production should populate the store so rotation does not need a redeploy.

    The SSA-specific name is tried first and must pair with ``APS_SSA_CLIENT_ID``: the robot API
    needs a Server-to-Server app, which cannot also serve the 3LO sign-in, so a deployment that
    has split the two would otherwise sign an SSA call with the login app's secret.
    """
    from backend.secrets import SecretNotFound, get_secret_store

    ref = app.get('client_secret_ref')
    if ref:
        try:
            return get_secret_store().get_secret(ref)
        except SecretNotFound:
            logger.info(
                'APS app %s has no secret at %s; falling back to APS_CLIENT_SECRET',
                app.get('app_ref'), ref,
            )
    env_secret = (
        os.getenv('APS_SSA_CLIENT_SECRET') or os.getenv('APS_CLIENT_SECRET') or ''
    ).strip()
    if not env_secret:
        raise ApsAppUnavailable(
            f'no client secret available for APS app {app.get("app_ref")} — store one at '
            f'{ref!r} or set APS_SSA_CLIENT_SECRET'
        )
    return env_secret


def robot_count(app_ref: str) -> int:
    return ssa_repository.count_by_app_ref(app_ref)


def has_capacity(app_ref: str) -> bool:
    app = aps_app_repository.get(app_ref)
    return bool(app) and robot_count(app_ref) < app['max_robots']


def pick_app_with_capacity() -> dict | None:
    """First active app with a free service-account slot, or None if all are full.

    Deterministic (aps_app_repository.list_active orders by app_ref) so a hub always lands on
    the lowest-numbered app that can take it. Warns ops on the way past 80%.
    """
    counts: dict[str, int] = {}
    for app in aps_app_repository.list_active():
        app_ref = app['app_ref']
        count = robot_count(app_ref)
        counts[app_ref] = count
        if count < app['max_robots']:
            if count / app['max_robots'] >= QUOTA_ALERT_THRESHOLD:
                ops_alert.send_ssa_quota_warning(
                    app_ref, app['client_id'], count, app['max_robots'],
                )
            return app
    ops_alert.send_ssa_capacity_exhausted(counts)
    return None


def get_app_for_hub(hub_id: str) -> dict:
    """The app a hub already provisions under — always from tenants.aps_app_ref.

    Never falls back to an environment variable or to "the first app": the robot's key was
    created under one specific Client ID and only that one can mint tokens for it.
    """
    tenant = tenant_repository.get(hub_id)
    if not tenant:
        raise ApsAppUnavailable(f'no tenant row for hub {hub_id}')
    app = aps_app_repository.get(tenant['aps_app_ref'])
    if not app:
        raise ApsAppUnavailable(
            f'hub {hub_id} references APS app {tenant["aps_app_ref"]}, which is not registered'
        )
    return app


def precheck_provision(hub_id: str) -> PreCheckResult:
    """Answer "can this hub be provisioned?" before calling APS.

    Checking first is what turns "enterprise 11 sees a raw 400 cs-16 from Autodesk" into a
    friendly, actionable panel (FR-06 §4.8 step 2, quota-blocked variant).
    """
    if ssa_repository.exists(hub_id):
        existing = ssa_repository.get_by_hub_id(hub_id)
        app = aps_app_repository.get(existing['aps_app_ref'])
        return PreCheckResult(
            status='already_provisioned',
            app_ref=existing['aps_app_ref'],
            client_id=(app or {}).get('client_id'),
        )

    app = pick_app_with_capacity()
    if app is None:
        return PreCheckResult(
            status='capacity_exhausted',
            message=(
                'Provisioning capacity reached. Our team has been notified — this hub '
                'cannot be set up until additional capacity is available.'
            ),
        )
    return PreCheckResult(
        status='ready',
        app_ref=app['app_ref'],
        client_id=app['client_id'],
        slots_remaining=app['max_robots'] - robot_count(app['app_ref']),
    )
