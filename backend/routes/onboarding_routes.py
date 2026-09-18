"""
Purpose: Hub onboarding HTTP surface (FR-03 §13) — service-account status, provisioning,
verification and key rotation, all scoped to the session's active hub.

Routes hold no business logic: they authorise, call a service, and shape the response
(ARCHITECTURE.md). The one thing they do own is turning a typed service exception into the
right status code and the right user-facing sentence — a capacity error and a permissions
error must not read the same to a hub admin.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify

from backend.repositories.state import ssa_repository, tenant_repository
from backend.services.m2m import (
    aps_app_service,
    hub_admin_service,
    provisioning_probe,
    ssa_provisioner,
)
from backend.utils import tenant_auth
from backend.utils.tenant_auth import tenant_route

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__)


def _status_payload(hub_id: str) -> dict:
    """Everything wizard steps 1 and 2 need, in one call."""
    tenant = tenant_repository.get(hub_id)
    credential = ssa_repository.get_by_hub_id(hub_id)
    precheck = aps_app_service.precheck_provision(hub_id)

    return {
        'hub_id': hub_id,
        'onboarding_status': (tenant or {}).get('onboarding_status'),
        'last_error': (tenant or {}).get('last_error'),
        # The Client ID the admin must whitelist for *this* hub. Resolved per hub rather than
        # from a global, so it stays correct if a second APS app is ever registered.
        'aps_client_id': precheck.client_id,
        'capacity': precheck.status,
        'slots_remaining': precheck.slots_remaining,
        'robot': {
            'email': credential['robot_email'],
            'key_id': credential['key_id'],
            'created_at': credential['created_at'],
            'rotated_at': credential['rotated_at'],
        } if credential else None,
    }


@onboarding_bp.route('/api/tenants/<hub_id>/ssa/status')
@tenant_route
def ssa_status(hub_id: str):
    tenant_auth.require_hub(hub_id)
    return jsonify(_status_payload(hub_id))


@onboarding_bp.route('/api/tenants/<hub_id>/ssa/provision', methods=['POST'])
@tenant_route
def ssa_provision(hub_id: str):
    """Idempotent. A hub that already has a robot returns it without calling APS."""
    user_id = tenant_auth.require_hub(hub_id)
    try:
        ssa_provisioner.ensure_ssa(hub_id, aps_user_id=user_id)
    except ssa_provisioner.SsaCapacityExhausted as exc:
        # 409, not 500: nothing is broken, there is simply no room until ops acts.
        return jsonify({
            'error': str(exc), 'reason': 'capacity_exhausted',
            'onboarding_status': 'ssa_limit_reached',
        }), 409
    except hub_admin_service.AccessDenied as exc:
        return jsonify({'error': str(exc), 'reason': exc.reason}), 403
    except ssa_provisioner.SsaProvisionError as exc:
        return jsonify({'error': str(exc), 'reason': 'provision_failed'}), 502
    return jsonify(_status_payload(hub_id))


@onboarding_bp.route('/api/tenants/<hub_id>/ssa/verify', methods=['POST'])
@tenant_route
def ssa_verify(hub_id: str):
    """Probe 1 — is our Client ID authorised on this hub's account (C1)?

    Needs no robot, so it can run as wizard step 1 before provisioning.
    """
    tenant_auth.require_hub(hub_id)
    result = provisioning_probe.verify_app_authorised(hub_id)
    if result.passed:
        # The missing whitelist is exactly what the account-admin probe fails on, and that
        # probe's answer is cached for HUB_ADMIN_CACHE_TTL_SEC. Without this, a real hub admin
        # who whitelists us and passes this check keeps being told they are not an admin until
        # the entry ages out. Clear the whole cache, not just this user's: the whitelist is
        # hub-wide, so every admin of the hub holds the same stale answer.
        hub_admin_service.invalidate()
    return jsonify(result.as_dict() | _status_payload(hub_id))


@onboarding_bp.route(
    '/api/tenants/<hub_id>/projects/<project_id>/verify', methods=['POST'],
)
@tenant_route
def verify_project(hub_id: str, project_id: str):
    """Probes 2 and 3 — is the robot on *this* project with Data Connector rights (C2/C3)?

    Per project, not per hub: this is wizard step 3's gate, and it must be asked again for
    every project the customer connects rather than inherited from the first one.
    """
    tenant_auth.require_hub(hub_id)
    result = provisioning_probe.verify_robot_on_project(hub_id, project_id)
    return jsonify(result.as_dict() | {'project_id': project_id})


@onboarding_bp.route('/api/tenants/<hub_id>/ssa/rotate-key', methods=['POST'])
@tenant_route
def ssa_rotate_key(hub_id: str):
    tenant_auth.require_hub(hub_id)
    try:
        ssa_provisioner.rotate_key(hub_id)
    except ssa_provisioner.SsaProvisionError as exc:
        return jsonify({'error': str(exc), 'reason': 'rotate_failed'}), 400
    return jsonify(_status_payload(hub_id))
