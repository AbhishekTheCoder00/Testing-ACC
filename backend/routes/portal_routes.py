"""
Purpose: The M2M Hub Sync portal's own HTTP surface (FR-06 §3) — serve the page, tell the
frontend who is signed in and which hubs they may administer, and switch hub context.

OAuth is deliberately not reimplemented: `/connect/acc` and `/callback` in acc_routes.py stay
untouched, and the portal simply asks to be returned to `/portal` afterwards via a session flag
that `app.index()` honours. That keeps the U2M sign-in path byte-identical while giving the
portal its own landing page.
"""

from __future__ import annotations

import logging

from urllib.parse import quote

from flask import Blueprint, jsonify, redirect, render_template, request, session

from backend.clients import acc_client
from backend.repositories.state import connection_repository, tenant_repository
from backend.services.m2m import aps_app_service, hub_admin_service
from backend.utils import tenant_auth
from backend.utils.tenant_auth import AuthError, tenant_route

logger = logging.getLogger(__name__)

portal_bp = Blueprint('portal', __name__)

POST_LOGIN_KEY = 'post_login_redirect'
PORTAL_PATH = '/portal'


@portal_bp.route('/portal')
def portal_page():
    return render_template('portal.html')


@portal_bp.route('/portal/login')
def portal_login():
    """Start ACC OAuth and ask to be brought back here rather than to the U2M wizard."""
    session[POST_LOGIN_KEY] = PORTAL_PATH
    return redirect('/connect/acc')


@portal_bp.route('/portal/databricks-login')
def portal_databricks_login():
    """Start Databricks OAuth for wizard step 3 and come back to the portal.

    Reuses `/connect/databricks-oauth` unchanged: its callback redirects to `/`, which already
    honours the same post-login flag the ACC path uses. No U2M route needed editing.
    """
    workspace_url = request.args.get('workspace_url', '').strip()
    if not workspace_url:
        return jsonify({'error': 'workspace_url is required'}), 400
    session[POST_LOGIN_KEY] = f'{PORTAL_PATH}?dbx=1'
    return redirect(f'/connect/databricks-oauth?workspace_url={quote(workspace_url)}')


@portal_bp.route('/api/session/logout', methods=['POST'])
def portal_logout():
    session.clear()
    return jsonify({'ok': True})


def _vendor_credentials() -> tuple[str, str]:
    """Client ID + secret of the APS app used for the account-admin probe."""
    active = aps_app_service.aps_app_repository.list_active()
    if not active:
        raise AuthError(
            503,
            'No APS application is registered. Set APS_CLIENT_ID and restart.',
            reason='not_configured',
        )
    app = active[0]
    return app['client_id'], aps_app_service.get_client_secret(app)


def _hub_summary(hub: dict) -> dict:
    """Shape one hub for the picker: business terms first, ids secondary (TC-CONN-14)."""
    hub_id = hub['id']
    tenant = tenant_repository.get(hub_id)
    connections = connection_repository.list_for_hub(hub_id) if tenant else []
    ready = [c for c in connections if c['onboarding_status'] == 'ready']
    return {
        'hub_id': hub_id,
        'name': hub.get('name') or hub_id,
        'id_short': hub_id[:12],
        'admin_status': hub.get('admin_status'),
        'onboarding_status': (tenant or {}).get('onboarding_status'),
        'connection_count': len(connections),
        'ready_count': len(ready),
        'needs_setup': tenant is None or (tenant or {}).get('onboarding_status') != 'ssa_active',
    }


@portal_bp.route('/api/me')
@tenant_route
def api_me():
    """Who is signed in, and which hubs they may administer.

    Denials are distinguished on purpose (FR-02 FR-01a and the outage case): a project admin
    gets Access Restricted, while an APS outage or a hub that has not whitelisted us yet gets a
    retryable message. Telling a real hub admin they are not one is the failure worth avoiding.
    """
    user_id = tenant_auth.require_user()

    try:
        hubs = acc_client.fetch_hubs(user_id)
    except Exception as exc:
        logger.warning('Hub listing failed for %s: %s', user_id, exc)
        return jsonify({
            'user_id': user_id,
            'access': 'unavailable',
            'reason': 'hub_listing_failed',
            'message': 'Could not reach Autodesk to list your hubs. Try again in a moment.',
            'hubs': [],
        }), 200

    client_id, client_secret = _vendor_credentials()
    try:
        admin_hubs = hub_admin_service.admin_hubs(
            user_id, hubs, client_id=client_id, client_secret=client_secret,
        )
    except hub_admin_service.AccessDenied as exc:
        # `unverified` means we could not ask, not that they lack the role — so the portal
        # shows the Custom Integration remedy, including the Client ID they need.
        payload = {
            'user_id': user_id,
            'access': 'unverified' if exc.reason == 'unverified' else 'denied',
            'reason': exc.reason,
            'message': str(exc),
            'hubs': [],
        }
        if exc.reason == 'unverified':
            payload['aps_client_id'] = client_id
        return jsonify(payload), 200

    return jsonify({
        'user_id': user_id,
        'access': 'ok',
        'hubs': [_hub_summary(h) for h in admin_hubs],
        'active_hub_id': tenant_auth.current_tenant_id(),
    })


@portal_bp.route('/api/session/hub', methods=['POST'])
@tenant_route
def api_select_hub():
    """Pick or switch the active hub, creating the user↔hub mapping on first selection.

    The mapping is written only after the admin check passes, so a non-admin never leaves a
    row behind (TC-AUTH-02/04).
    """
    user_id = tenant_auth.require_user()
    hub_id = (request.json or {}).get('hub_id', '').strip()
    if not hub_id:
        raise AuthError(400, 'hub_id is required.', reason='bad_request')

    hubs = acc_client.fetch_hubs(user_id)
    match = next((h for h in hubs if h['id'] == hub_id), None)
    if not match:
        raise AuthError(403, 'That hub is not available to you.', reason='not_visible')

    client_id, client_secret = _vendor_credentials()
    status = hub_admin_service.check_hub_admin(
        user_id, hub_id, client_id=client_id, client_secret=client_secret,
    )
    if status == hub_admin_service.NOT_ADMIN:
        raise AuthError(403, 'You are not an administrator of this hub.',
                        reason='not_hub_admin')
    if status == hub_admin_service.UNVERIFIED and \
            hub_admin_service.enforcement_mode() == hub_admin_service.ENFORCEMENT_STRICT:
        raise AuthError(
            403,
            'Add the connector Client ID under Account Admin ▸ Settings ▸ Custom '
            'Integrations, then try again.',
            reason='unverified',
        )

    # Map the user only once a tenant exists. The tenant row is created by ensure_ssa, which
    # also assigns the APS app; mapping earlier would need an app_ref we have not picked yet.
    tenant = tenant_repository.get(hub_id)
    if tenant:
        tenant_repository.add_user(hub_id, user_id)

    tenant_auth.set_active_hub(hub_id)
    return jsonify({
        'ok': True,
        'hub_id': hub_id,
        'hub_name': match.get('name'),
        'onboarding_status': (tenant or {}).get('onboarding_status'),
    })
