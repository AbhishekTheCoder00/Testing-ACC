"""
Purpose: Provisions exactly one SSA robot per hub, idempotently (FR-02 FR-09…FR-13, FR-28).
This is the module that must never create a second robot for a hub: the APS quota is ten per
Client ID across every customer, so a duplicate is not a tidiness problem but lost capacity.

Three guards, in order of how much they cost to get wrong:
  * CS-01 — an existing credential row short-circuits before any APS call is made.
  * UNIQUE(hub_id) — two admins clicking Provision at the same instant resolve to one robot.
  * orphan adoption — if APS created the robot but the vault write or the insert then failed,
    the robot id is already recorded on the tenant and the retry reuses it.

Creating a robot needs only our own 2-legged app token and does not touch the customer's hub,
so no whitelist gate belongs here (ADR.md, Phase 1 D-16). The hub-admin check does, because it
is the only thing standing between a non-admin and one of those ten slots.
"""

from __future__ import annotations

import logging
import time

from backend.clients.acc import ssa_client
from backend.clients.acc.ssa_client import SsaQuotaExceeded
from backend.repositories.state import ssa_repository, tenant_repository
from backend.secrets import get_secret_store, ssa_private_key_ref
from backend.services.m2m import aps_app_service, hub_admin_service

logger = logging.getLogger(__name__)


class SsaProvisionError(RuntimeError):
    """Provisioning failed for a reason the caller should surface to the admin."""


class SsaCapacityExhausted(SsaProvisionError):
    """Every APS app is at its service-account limit — this hub cannot be onboarded."""


def _integrity_error() -> tuple[type[Exception], ...]:
    """The duplicate-key exception types, whichever backend is active."""
    import sqlite3

    types: list[type[Exception]] = [sqlite3.IntegrityError]
    try:  # psycopg is only installed when CONN_STRING is used
        import psycopg

        types.append(psycopg.errors.UniqueViolation)
    except Exception:
        pass
    return tuple(types)


def _mark_tenant_failed(hub_id: str, status: str, error: str) -> None:
    """Record a provisioning failure on the tenant, if there is one yet.

    A brand-new hub blocked by capacity has no tenant row and must not get one: the row
    requires an ``aps_app_ref`` and there is no app with room to assign.
    """
    if tenant_repository.get(hub_id):
        tenant_repository.update_status(hub_id, status, last_error=error)


def _resolve_app(hub_id: str) -> dict:
    """Which APS app this hub provisions under.

    An existing tenant keeps its app forever — the assignment is immutable because the robot's
    key only works under the Client ID it was created with. Only a new hub consults capacity.
    """
    if tenant_repository.get(hub_id):
        return aps_app_service.get_app_for_hub(hub_id)

    result = aps_app_service.precheck_provision(hub_id)
    if result.status == 'capacity_exhausted':
        raise SsaCapacityExhausted(result.message)
    app = aps_app_service.aps_app_repository.get(result.app_ref)
    if not app:
        raise SsaProvisionError(
            'No APS application is registered. Set APS_CLIENT_ID and restart so init_db '
            'seeds the aps_apps registry.'
        )
    return app


def ensure_ssa(hub_id: str, *, aps_user_id: str | None = None) -> dict:
    """Return this hub's robot, creating it on first call. Idempotent.

    ``aps_user_id`` enables the hub-admin check. It is optional only so the scheduler and
    internal callers, which have no session, can resolve credentials for a hub that was already
    provisioned by a human — for those the CS-01 short circuit returns before the check.
    """
    existing = ssa_repository.get_by_hub_id(hub_id)
    if existing:
        return existing  # CS-01 — no APS call, ever

    app = _resolve_app(hub_id)

    # The only real-resource gate. Blocks a non-admin before they can consume a slot.
    if aps_user_id:
        hub_admin_service.require_conclusive_admin(
            aps_user_id, hub_id,
            client_id=app['client_id'],
            client_secret=aps_app_service.get_client_secret(app),
        )

    tenant_repository.ensure_tenant(hub_id, app['app_ref'])

    # Cross-worker mutex. Without it, concurrent admins each reach POST /service-accounts and
    # create a robot apiece — only one can be stored, and the rest silently consume slots.
    if not tenant_repository.try_claim_provisioning(hub_id):
        loser = _await_winner(hub_id)
        if loser:
            return loser
        raise SsaProvisionError(
            f'Service account provisioning is already in progress for hub {hub_id}. '
            'Try again in a moment.'
        )

    try:
        # Double-checked locking. The CS-01 check at the top of this function may have run
        # before another admin's insert landed, and the claim only became free because that
        # admin finished and released it — so without re-reading here we would create a
        # second robot for a hub that already has one.
        already = ssa_repository.get_by_hub_id(hub_id)
        if already:
            return already
        return _create_robot(hub_id, app)
    except SsaQuotaExceeded as exc:
        # APS disagreed with our pre-check — another process took the last slot.
        message = (
            'Provisioning capacity reached. Our team has been notified — this hub cannot '
            'be set up until additional capacity is available.'
        )
        _mark_tenant_failed(hub_id, 'ssa_limit_reached', str(exc))
        raise SsaCapacityExhausted(message) from exc
    except _integrity_error():
        # Two admins raced. The other one won; return what they created.
        winner = ssa_repository.get_by_hub_id(hub_id)
        if winner:
            logger.info('Concurrent ensure_ssa for hub %s — reusing the winner\'s robot', hub_id)
            return winner
        raise
    except SsaProvisionError:
        raise
    except Exception as exc:
        _mark_tenant_failed(hub_id, 'ssa_provision_failed', str(exc))
        raise SsaProvisionError(f'SSA provisioning failed for hub {hub_id}: {exc}') from exc
    finally:
        tenant_repository.release_provisioning(hub_id)


def _await_winner(hub_id: str, attempts: int = 40, delay: float = 0.25) -> dict | None:
    """Wait briefly for the admin who won the claim to finish, then use their robot.

    Better than returning an error to the second admin: the outcome they wanted is about to
    exist. Bounded so a stuck winner surfaces as a retryable message rather than a hang.
    """
    for _ in range(attempts):
        existing = ssa_repository.get_by_hub_id(hub_id)
        if existing:
            return existing
        time.sleep(delay)
    return ssa_repository.get_by_hub_id(hub_id)


def _create_robot(hub_id: str, app: dict) -> dict:
    """Create (or adopt) the robot and store its key. Raises on any failure."""
    client_secret = aps_app_service.get_client_secret(app)
    admin_token = ssa_client.get_admin_token(app['client_id'], client_secret)

    tenant = tenant_repository.get(hub_id) or {}
    adopted = tenant.get('pending_service_account_id')
    if adopted:
        # A previous attempt got as far as creating the robot. Reuse it rather than spend
        # another of the ten slots; only the key is missing.
        logger.warning(
            'Adopting orphaned SSA robot %s for hub %s from a previous failed attempt',
            adopted, hub_id,
        )
        service_account_id = adopted
        robot_email = tenant.get('pending_robot_email') or ''
    else:
        account = ssa_client.create_service_account(admin_token, hub_id=hub_id)
        service_account_id = account.service_account_id
        robot_email = account.email
        # Recorded before the key exists — this is what makes the retry above possible.
        tenant_repository.set_pending_robot(hub_id, service_account_id, robot_email)

    key = ssa_client.create_key(admin_token, service_account_id)
    key_ref = get_secret_store().put_secret(
        ssa_private_key_ref(hub_id),
        key.private_key_pem,
        description=f'SSA private key for hub {hub_id}',
    )

    credential = ssa_repository.insert(
        hub_id=hub_id,
        aps_app_ref=app['app_ref'],
        service_account_id=service_account_id,
        robot_email=robot_email,
        key_id=key.kid,
        private_key_ref=key_ref,
    )
    tenant_repository.clear_pending_robot(hub_id)
    # Re-assert the current status purely to clear `last_error` from an earlier failed attempt.
    # Must not hard-code `pending_whitelist`: the wizard verifies the whitelist in step 1 and
    # provisions here in step 2, so writing that back would undo the verification and bounce the
    # admin to step 1 instead of showing them the robot email to invite.
    tenant = tenant_repository.get(hub_id) or {}
    tenant_repository.update_status(
        hub_id, tenant.get('onboarding_status') or 'pending_whitelist',
    )
    logger.info('Hub %s provisioned robot %s', hub_id, robot_email)
    return credential


def rotate_key(hub_id: str) -> dict:
    """Replace the hub's signing key with no downtime (FR-03 §12.3).

    New key first, old key deleted last, so a failure part-way leaves a hub that can still
    mint tokens. The robot identity never changes — rotation must not create a robot.
    """
    credential = ssa_repository.get_by_hub_id(hub_id)
    if not credential:
        raise SsaProvisionError(f'hub {hub_id} has no service account to rotate')

    app = aps_app_service.get_app_for_hub(hub_id)
    client_secret = aps_app_service.get_client_secret(app)
    admin_token = ssa_client.get_admin_token(app['client_id'], client_secret)
    old_key_id = credential['key_id']

    new_key = ssa_client.create_key(admin_token, credential['service_account_id'])
    key_ref = get_secret_store().put_secret(
        ssa_private_key_ref(hub_id),
        new_key.private_key_pem,
        description=f'SSA private key for hub {hub_id} (rotated)',
    )
    ssa_repository.update_key(hub_id, key_id=new_key.kid, private_key_ref=key_ref)

    # Drop any cached token minted with the old key before proving the new one works.
    from backend.services.m2m import ssa_token_service

    ssa_token_service.invalidate(hub_id)
    ssa_token_service.get_acc_token(hub_id, force=True)

    if old_key_id and old_key_id != new_key.kid:
        try:
            ssa_client.delete_key(admin_token, credential['service_account_id'], old_key_id)
        except Exception as exc:
            # The rotation itself succeeded; a stale key is an ops cleanup, not a failure.
            logger.warning(
                'Rotated hub %s to key %s but could not delete old key %s: %s',
                hub_id, new_key.kid, old_key_id, exc,
            )
    return ssa_repository.get_by_hub_id(hub_id)
