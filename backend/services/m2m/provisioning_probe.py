"""
Purpose: Proves the customer completed the manual ACC steps, because Autodesk exposes no API
to ask "was this invited?" — the answer is inferred from how real API calls respond
(FR-03 §18 Q1).

Two functions, not FR-03 §8.2's one, because the two manual steps differ in both scope and
credential:

  * C1 — the *Client ID* is in Custom Integrations. Authorises our application. One answer per
    hub, checked with a 2-legged token, and crucially **needs no robot**, so it can gate wizard
    step 1 before provisioning (FR-06 §4.8).
  * C2/C3 — the *robot* is on the project with Data Connector rights. One answer **per
    project**, checked with the hub's SSA token. §8.2 cached this per hub, which silently
    skipped the check on a customer's second project and failed at first sync instead of at
    setup.

Probe 3 is the one that actually proves the chain end to end (there is no API for "is the robot
subscribed to Insight"); probes 1 and 2 exist so a failure can say *which* link is missing.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import requests

from backend.clients.acc.constants import APS_BASE_DM, DC_BASE
from backend.clients.acc.http_client import _get
from backend.repositories.state import ssa_repository, tenant_repository
from backend.services.m2m import aps_app_service, ssa_token_service

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30

CUSTOM_INTEGRATION_PATH = 'Account Admin ▸ Settings ▸ Custom Integrations'


@dataclass
class ProbeResult:
    passed: bool
    code: str
    message: str
    remediation: str = ''
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _status_of(exc: Exception) -> int | None:
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    if status:
        return int(status)
    text = str(exc)
    for candidate in (401, 403, 404):
        if str(candidate) in text:
            return candidate
    return None


# ---------------------------------------------------------------------------
# C1 — application authorised on the hub. 2-legged, no robot.
# ---------------------------------------------------------------------------


def verify_app_authorised(hub_id: str) -> ProbeResult:
    """Probe 1. A 403 here means our Client ID is not in Custom Integrations.

    Uses a 2-legged token on purpose: the answer is about the *application*, so the robot
    contributes nothing and requiring one would deadlock wizard step 1 against step 2.
    """
    try:
        client_id, secret = _vendor_credentials(hub_id)
    except Exception as exc:
        return ProbeResult(False, 'not_configured', str(exc))

    from backend.clients.acc import admin_client

    try:
        # data:read, not account:read — this probe calls Data Management, which refuses an
        # Account-Admin-scoped token with a 403 indistinguishable from "not whitelisted".
        token = admin_client.get_account_token(
            client_id, secret, scope=admin_client.DATA_SCOPE,
        )
        _get(f'{APS_BASE_DM}/hubs/{hub_id}/projects', token)
    except Exception as exc:
        status = _status_of(exc)
        if status in (401, 403, 404):
            return ProbeResult(
                False, 'client_id_not_whitelisted',
                'Autodesk refused this request, which almost always means the connector '
                'Client ID has not been added to this hub yet.',
                remediation=(
                    f'In ACC, go to {CUSTOM_INTEGRATION_PATH} and add Client ID '
                    f'{client_id}. Then click Verify again.'
                ),
                detail={'aps_client_id': client_id, 'status': status},
            )
        return ProbeResult(
            False, 'unavailable',
            'Could not reach Autodesk to check this hub. Try again in a moment.',
            detail={'error': str(exc)[:300]},
        )

    _mark_app_authorised(hub_id)
    return ProbeResult(
        True, 'ok', 'Custom Integration verified — the connector is authorised on this hub.',
        detail={'aps_client_id': client_id},
    )


def _vendor_credentials(hub_id: str) -> tuple[str, str]:
    """The Client ID for this hub — its own app once assigned, otherwise the one we would use."""
    if tenant_repository.get(hub_id):
        app = aps_app_service.get_app_for_hub(hub_id)
    else:
        precheck = aps_app_service.precheck_provision(hub_id)
        if not precheck.app_ref:
            raise RuntimeError(
                'No APS application with capacity is available for this hub.'
            )
        app = aps_app_service.aps_app_repository.get(precheck.app_ref)
    return app['client_id'], aps_app_service.get_client_secret(app)


def _mark_app_authorised(hub_id: str) -> None:
    """Record the pass, without ever moving the status backwards."""
    tenant = tenant_repository.get(hub_id)
    if not tenant:
        # Step 1 runs before ensure_ssa, so a first-time hub has no row yet and returning
        # here would discard the pass — the wizard re-derives its step from the persisted
        # status and would send the admin back to step 1 forever. Pin the shard to the app
        # whose Client ID was just verified, too: a later capacity pick could otherwise
        # choose an app this hub never whitelisted.
        precheck = aps_app_service.precheck_provision(hub_id)
        if not precheck.app_ref:
            return
        tenant = tenant_repository.ensure_tenant(hub_id, precheck.app_ref)
    tenant_repository.set_ssa_verified(hub_id)
    if tenant['onboarding_status'] in ('pending_whitelist', 'ssa_provision_failed'):
        tenant_repository.update_status(hub_id, 'whitelist_verified')


# ---------------------------------------------------------------------------
# C2 + C3 — robot on the project, with Data Connector rights. SSA token, per project.
# ---------------------------------------------------------------------------


def verify_robot_on_project(hub_id: str, project_id: str) -> ProbeResult:
    """Probes 2 and 3 for one project.

    Per project, never cached per hub: the robot must be invited to every project a customer
    connects (FR-03 §18 Q4), so a hub-level answer would wave through the second one.
    """
    credential = ssa_repository.get_by_hub_id(hub_id)
    if not credential:
        return ProbeResult(
            False, 'no_robot', 'This hub has no service account yet.',
            remediation='Provision the service account first.',
        )

    try:
        token = ssa_token_service.get_acc_token(hub_id)
    except Exception as exc:
        return ProbeResult(
            False, 'token_mint_failed',
            'Could not obtain a token for this hub\'s service account.',
            detail={'error': str(exc)[:300]},
        )

    robot_email = credential['robot_email']

    # Probe 2 — is the robot a member of this project?
    try:
        _get(f'{APS_BASE_DM}/hubs/{hub_id}/projects/{project_id}', token)
    except Exception as exc:
        status = _status_of(exc)
        if status in (401, 403, 404):
            return ProbeResult(
                False, 'robot_not_on_project',
                'The service account cannot see this project.',
                remediation=(
                    f'In ACC, invite {robot_email} to this project as a member and give it '
                    'project admin rights, then click Verify again.'
                ),
                detail={'robot_email': robot_email, 'status': status},
            )
        return ProbeResult(
            False, 'unavailable',
            'Could not reach Autodesk to check the project. Try again in a moment.',
            detail={'error': str(exc)[:300]},
        )

    # Probe 3 — can the robot actually use Data Connector? This is the one that proves the
    # whole chain, including Insight module access, which has no API of its own.
    dc = _probe_data_connector(token, hub_id, project_id, robot_email)
    if not dc.passed:
        return dc

    return ProbeResult(
        True, 'ok',
        'Service account verified on this project.',
        detail={'robot_email': robot_email},
    )


def _probe_data_connector(token: str, hub_id: str, project_id: str,
                          robot_email: str) -> ProbeResult:
    """Read-only Data Connector call as the robot. A 403 means missing DC/Insight rights."""
    from backend.clients.acc.admin_client import account_id_for_hub

    account_id = account_id_for_hub(hub_id)
    bare_project = project_id[2:] if project_id.startswith('b.') else project_id
    url = f'{DC_BASE}/accounts/{account_id}/requests'
    try:
        requests_resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {token}'},
            params={'projectId': bare_project, 'limit': 1},
            timeout=_HTTP_TIMEOUT,
        )
    except Exception as exc:
        return ProbeResult(
            False, 'unavailable',
            'Could not reach the Data Connector API. Try again in a moment.',
            detail={'error': str(exc)[:300]},
        )

    if requests_resp.status_code in (401, 403):
        return ProbeResult(
            False, 'robot_missing_data_connector',
            'The service account is on the project but cannot use Data Connector.',
            remediation=(
                f'In ACC, give {robot_email} account admin rights and access to the Insight '
                'module, which is what Data Connector exports run under.'
            ),
            detail={'robot_email': robot_email, 'status': requests_resp.status_code},
        )
    if requests_resp.status_code >= 500:
        return ProbeResult(
            False, 'unavailable',
            'Data Connector is not responding. Try again in a moment.',
            detail={'status': requests_resp.status_code},
        )
    return ProbeResult(True, 'ok', 'Data Connector access confirmed.')
