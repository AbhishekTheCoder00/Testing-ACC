
import base64
import hashlib
import logging
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse
from flask import Flask, abort, jsonify, redirect, request, session
from backend.repositories import state_store as db
from backend.config import get_config
from flask import Blueprint

databricks_bp = Blueprint(
    "databricks",
    __name__
)

from backend.clients.databricks_client import DatabricksClient


# Zerobus prerequisite diagnostics only (not a sync writer). Loaded when
# ENABLE_ZEROBUS=true; conditional import so flag-off deployments skip it.
_ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
if _ENABLE_ZEROBUS:
    # from backend import zerobus_status
    from backend.services import zerobus_service
else:
    zerobus_service= None  # sentinel — guards every site that referenced the module
    # zerobus_status = None  # sentinel — guards every site that referenced the module

# # logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

logger = logging.getLogger(__name__)

# Multi-tenant session: identity is derived at /callback from the APS userinfo
# response and stored in session['user_id']. The signed Flask cookie keeps
# concurrent pilot users isolated without a separate user-account table.
# 1-day rolling idle timeout — every authenticated request resets the clock,
# so a user who keeps using the connector stays signed in; an idle browser
# is logged out after 24 hours. Tokens themselves still refresh on their own
# ACC/Databricks expiry timers.
# app.permanent_session_lifetime = timedelta(days=1)

from backend.utils.auth import _current_user_id, _require_user_id

from backend.utils.databricks_auth import (
    DBX_OAUTH_SCOPE,
    _detect_cloud_provider,
    _resolve_databricks_oidc_endpoints,
    create_databricks_client_for_user,
)

# The Databricks OAuth authorize callback is fixed for the whole deployment
# (the URL a user pastes into Databricks as the app's Redirect URL). It is NOT
# collected per-user. An explicit DATABRICKS_REDIRECT_URI env var overrides it.
DATABRICKS_REDIRECT_URI = os.getenv(
    'DATABRICKS_REDIRECT_URI', 'https://formabricks-stg.cctech.co.in/databricks/callback'
)



@databricks_bp.route('/connect/databricks/save-credentials', methods=['POST'])
def save_databricks_credentials():
    """Persist the ACC user's Databricks OAuth app credentials.

    One row per ACC user_id in ``app_secrets``. Replaces the old per-email
    registration form; credentials are entered inline in the Connect Databricks
    panel before the OAuth sign-in is attempted.
    """
    uid = _require_user_id()
    payload = request.get_json(silent=True) or {}
    values = {
        'DATABRICKS_WORKSPACE_URL': str(payload.get('workspace_url') or '').strip(),
        'DATABRICKS_CLIENT_ID':     str(payload.get('client_id') or '').strip(),
        'DATABRICKS_CLIENT_SECRET': str(payload.get('client_secret') or '').strip(),
    }
    if not values['DATABRICKS_WORKSPACE_URL']:
        return jsonify({'ok': False, 'error': 'Databricks Workspace URL is required.'}), 400
    if not values['DATABRICKS_CLIENT_ID']:
        return jsonify({'ok': False, 'error': 'Databricks Client ID is required.'}), 400
    if not values['DATABRICKS_CLIENT_SECRET']:
        return jsonify({'ok': False, 'error': 'Databricks Client Secret is required.'}), 400
    db.save_credentials(uid, values)
    return jsonify({'ok': True})


@databricks_bp.route('/connect/databricks/status')
def databricks_credentials_status():
    """Report whether the current ACC user has saved Databricks credentials."""
    uid = _current_user_id()
    saved = bool(uid) and db.has_credentials(uid)
    workspace_url = ''
    if saved:
        workspace_url = db.get_secret(uid, 'DATABRICKS_WORKSPACE_URL') or ''
    return jsonify({
        'saved': saved,
        'workspace_url': workspace_url,
        'user_id': uid or '',
        'redirect_uri': DATABRICKS_REDIRECT_URI,
    })


@databricks_bp.route('/connect/databricks/delete', methods=['POST'])
def delete_databricks_credentials():
    """Delete the current ACC user's saved Databricks credentials."""
    uid = _require_user_id()
    db.clear_credentials(uid)
    return jsonify({'ok': True})


@databricks_bp.route('/connect/databricks-oauth')
def connect_databricks_oauth():
    """Redirect the user to Databricks OIDC authorize endpoint."""
    raw_workspace_url = request.args.get('workspace_url', '').strip()
    if not raw_workspace_url:
        return '<p>workspace_url parameter is required.</p><p><a href="/">Go back</a></p>', 400

    # Sanitize: users often paste the URL from their workspace address bar,
    # which includes /browse?o=... or other path/query cruft. Keep only scheme + host.
    if '://' not in raw_workspace_url:
        raw_workspace_url = 'https://' + raw_workspace_url
    parsed = urlparse(raw_workspace_url)
    if not parsed.scheme or not parsed.netloc:
        return '<p>Invalid workspace URL. Expected something like https://adb-XXXX.X.azuredatabricks.net</p><p><a href="/">Go back</a></p>', 400
    workspace_url = f'{parsed.scheme}://{parsed.netloc}'

    client_id    = get_config('DATABRICKS_CLIENT_ID', '')
    redirect_uri = DATABRICKS_REDIRECT_URI

    if not client_id:
        return '<p>DATABRICKS_CLIENT_ID must be set in .env</p>', 500

    # CSRF protection — store state + workspace in server-side session
    state = secrets.token_urlsafe(32)
    session['dbx_oauth_state']     = state
    session['dbx_workspace_url']   = workspace_url

    # PKCE — verifier stays server-side (never leaves session); only the SHA-256
    # challenge travels over the wire. Required by Databricks Partner Well-Architected
    # Framework for U2M flows. Verifier length 43-128 chars per RFC 7636.
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).rstrip(b'=').decode('ascii')
    session['dbx_pkce_verifier'] = code_verifier

    auth_url, _ = _resolve_databricks_oidc_endpoints(workspace_url)
    full_url = (
        f'{auth_url}'
        f'?client_id={quote(client_id, safe="")}'
        f'&response_type=code'
        f'&redirect_uri={quote(redirect_uri, safe="")}'
        f'&scope={quote(DBX_OAUTH_SCOPE, safe="")}'
        f'&state={quote(state, safe="")}'
        f'&code_challenge={quote(code_challenge, safe="")}'
        f'&code_challenge_method=S256'
    )
    return redirect(full_url)


@databricks_bp.route('/databricks/callback')
def databricks_callback():
    """Exchange Databricks OIDC authorization code for tokens; user starts provisioning from Step 2."""
    code       = request.args.get('code')
    error      = request.args.get('error')
    error_desc = request.args.get('error_description', '')
    state      = request.args.get('state', '')

    if error:
        logger.error('Databricks OAuth error: %s — %s', error, error_desc)
        return f'<p>Databricks auth error: {error}</p><p>{error_desc}</p><p><a href="/">Go back</a></p>', 400
    if not code:
        return '<p>No authorization code received.</p><p><a href="/">Go back</a></p>', 400

    # CSRF validation
    expected_state = session.pop('dbx_oauth_state', None)
    if not expected_state or state != expected_state:
        logger.error('Databricks OAuth state mismatch — possible CSRF')
        return '<p>Invalid OAuth state. Please try again.</p><p><a href="/">Go back</a></p>', 400

    workspace_url = session.pop('dbx_workspace_url', '').strip().rstrip('/')
    if not workspace_url:
        return '<p>Workspace URL missing from session. Please try again.</p><p><a href="/">Go back</a></p>', 400

    # PKCE — retrieve the verifier paired with the challenge sent at /connect/databricks-oauth.
    # If the challenge was sent (new flows always do), the verifier MUST be present here or
    # Databricks will reject the exchange with invalid_grant.
    code_verifier = session.pop('dbx_pkce_verifier', None)

    client_id     = get_config('DATABRICKS_CLIENT_ID', '')
    client_secret = get_config('DATABRICKS_CLIENT_SECRET', '')
    redirect_uri  = DATABRICKS_REDIRECT_URI

    try:
        import requests as req
        _, token_url = _resolve_databricks_oidc_endpoints(workspace_url)
        token_payload = {
            'grant_type':    'authorization_code',
            'client_id':     client_id,
            'client_secret': client_secret,
            'code':          code,
            'redirect_uri':  redirect_uri,
        }
        if code_verifier:
            token_payload['code_verifier'] = code_verifier
        token_resp = req.post(
            token_url,
            data=token_payload,
            timeout=15,
        )
        if not token_resp.ok:
            logger.error('Databricks token exchange failed: %s', token_resp.text[:500])
            return f'<p>Token exchange failed: {token_resp.text[:300]}</p><p><a href="/">Go back</a></p>', 500

        tokens        = token_resp.json()
        access_token  = tokens['access_token']
        refresh_token = tokens.get('refresh_token')
        expires_in    = tokens.get('expires_in', 3600)
        cloud_provider = _detect_cloud_provider(workspace_url)
        uid = _require_user_id()
        db.save_dbx_tokens(uid, workspace_url, access_token, refresh_token, expires_in,
                           cloud_provider=cloud_provider)

        validate_resp = req.get(
            f'{workspace_url}/api/2.0/clusters/spark-versions',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        if validate_resp.status_code != 200:
            logger.error('Databricks workspace validation failed: %s', validate_resp.text[:300])
            return '<p>Databricks token valid but workspace access denied. Check permissions.</p><p><a href="/">Go back</a></p>', 401

        # Databricks is the second leg — ACC must already have established
        # session['user_id'] in /callback. If somebody hits /databricks/callback
        # directly without ACC, we abort rather than silently creating an
        # orphaned dbx_tokens row under no user.
        uid = _require_user_id()
        db.save_dbx_tokens(uid, workspace_url, access_token, refresh_token, expires_in,cloud_provider=cloud_provider)
        logger.info('Databricks OIDC tokens saved — user=%s cloud=%s', uid, cloud_provider)

    except Exception as e:
        logger.error('Databricks callback error: %s', e)
        return f'<p>Databricks OAuth error: {e}</p><p><a href="/">Go back</a></p>', 500

    return redirect('/?dbx_connected=1')


# @databricks_bp.route('/bootstrap/status')
# def bootstrap_status():
#     uid = _require_user_id()
#     progress = bs.get_progress(uid)
#     return jsonify(progress)


# Catalog kinds that can never host a table we write. Filtering them costs no
# API call and drops `system`, `samples` and every Delta Share from the picker.
_READONLY_CATALOG_TYPES = {
    'SYSTEM_CATALOG',
    'FOREIGN_CATALOG',
    'INTERNAL_CATALOG',
    'DELTASHARING_CATALOG',
    'DELTA_SHARING_CATALOG',
}

# Either privilege on the catalog is enough for provisioning: bootstrap creates
# <catalog>.bronze, so CREATE_SCHEMA is the gate — CREATE_TABLE, CREATE_VOLUME
# and MODIFY all inherit down from the catalog once the schema is ours.
_WRITE_PRIVILEGES = {'ALL_PRIVILEGES', 'CREATE_SCHEMA'}


def _can_write_tables(client, catalog_name: str, principal: str) -> bool:
    """True when ``principal`` may create the bronze schema in this catalog.

    Uses UC *effective* permissions so grants inherited through a group count.
    Fails open: a permissions call we cannot make must never hide a catalog the
    user can actually use. Provisioning would then fail with a real Databricks
    error, which is far easier to debug than a catalog that silently vanished.
    """
    try:
        data = client.uc_get_permissions(
            'catalog', catalog_name, principal=principal, effective=True,
        )
    except Exception as exc:
        logger.info('Write-permission check skipped for %s: %s', catalog_name, exc)
        return True
    granted = {
        str(priv).upper().replace(' ', '_')
        for entry in data.get('privilege_assignments') or []
        for priv in entry.get('privileges') or []
    }
    return bool(granted & _WRITE_PRIVILEGES)


def _dbx_catalogs_reauth_response(exc: Exception):
    """Map missing/expired U2M tokens to a 401 the UI can turn into re-login."""
    msg = str(exc)
    if 'not connected' in msg.lower():
        return jsonify({'error': 'Databricks not connected', 'catalogs': []}), 401
    return jsonify({
        'error': 'Databricks session expired — sign in again.',
        'catalogs': [],
        'reauth_required': True,
    }), 401


@databricks_bp.route('/databricks/catalogs')
def databricks_catalogs():
    """List the Unity Catalogs the signed-in user can actually provision into.

    "Visible" is not enough: the list API returns every catalog the token can
    see, including BROWSE-only ones. Provisioning writes tables, so read-only
    catalog kinds are dropped outright and the rest are checked for the
    privilege that lets us create <catalog>.bronze.

    Returns name, comment, catalog_type, storage_root, and a human-friendly
    storage_label per catalog. The ``zerobus_eligible`` field is included
    only when ``ENABLE_ZEROBUS=true`` — Zerobus is otherwise dormant in the
    connector and surfacing the readiness flag would only confuse the UI.

    Each catalog also carries a provisioning ``status``:
      available     — unclaimed and writable, anyone may provision it
      owned         — claimed by the caller; their pipeline names are included
      locked        — claimed by someone else; selectable by nobody but them
      no_permission — writable kind, but the user lacks CREATE SCHEMA on it

    ``locked: true`` means "not selectable" for any of those reasons; the
    ``status`` says which. Taken entries deliberately carry no owner identity —
    the UI only needs to know the catalog is unavailable, not to whom.
    """
    uid = _require_user_id()
    try:
        client = create_databricks_client_for_user(uid)
    except RuntimeError as exc:
        logger.warning('List catalogs — token unavailable: %s', exc)
        return _dbx_catalogs_reauth_response(exc)

    try:
        raw = client.uc_list_catalogs()
        workspace_key = db.normalize_workspace_key(client.base)
        claims_by_catalog = {
            c['catalog_name']: c for c in db.list_active_claims(workspace_key)
        }
        catalogs = []
        owners = {}
        for c in raw:
            name = c.get('name')
            if not name:
                continue
            catalog_type = (c.get('catalog_type') or '').upper()
            if catalog_type in _READONLY_CATALOG_TYPES:
                continue  # read-only kind — no table can ever be written here
            owners[name] = (c.get('owner') or '').strip().lower()
            storage_root = (c.get('storage_root') or '').strip() or None
            # Managed catalog with explicit storage_root is the only shape
            # Zerobus accepts. Computed even with the flag off so the
            # storage_label stays consistent for the UI.
            managed_with_storage = bool(
                storage_root and catalog_type in ('', 'MANAGED_CATALOG')
            )
            if managed_with_storage:
                storage_label = 'Managed (explicit storage)'
            elif catalog_type == 'SYSTEM_CATALOG':
                storage_label = 'System'
            elif catalog_type == 'FOREIGN_CATALOG':
                storage_label = 'Foreign'
            elif catalog_type in ('DELTASHARING_CATALOG', 'DELTA_SHARING_CATALOG'):
                storage_label = 'Delta Sharing'
            elif not storage_root:
                storage_label = 'Default Storage'
            else:
                storage_label = catalog_type.title().replace('_', ' ') or 'Unknown'

            claim = claims_by_catalog.get(name)
            if claim is None:
                status = 'available'
            elif claim['owner_user_id'] == uid:
                status = 'owned'
            else:
                status = 'locked'

            entry = {
                'name':          name,
                'comment':       c.get('comment') or '',
                'catalog_type':  catalog_type or None,
                'storage_root':  storage_root,
                'storage_label': storage_label,
                'status':        status,
                'locked':        status == 'locked',
            }
            if status == 'owned':
                entry['snapshot_pipeline_name'] = claim.get('snapshot_pipeline_name')
                entry['cdc_pipeline_name']      = claim.get('cdc_pipeline_name')
            if _ENABLE_ZEROBUS:
                entry['zerobus_eligible'] = managed_with_storage
            catalogs.append(entry)

        # Mark the catalogs the user cannot write to. Owners hold every
        # privilege implicitly, so an owner match needs no call; 'owned'
        # catalogs are already provisioned by this user and must never be
        # taken away by a permissions blip. If we cannot resolve who the
        # caller is, skip the whole pass rather than mislabel everything.
        try:
            me = (client.get_current_user_email() or '').strip().lower()
        except Exception as exc:
            logger.info('Skipping write-permission filter — no identity: %s', exc)
            me = ''
        pending = [
            e for e in catalogs
            if me and e['status'] == 'available' and owners.get(e['name']) != me
        ]
        if pending:
            # ponytail: one effective-permissions GET per candidate catalog,
            # 8 at a time. Fine into the low hundreds; cache per session if a
            # workspace ever has thousands.
            with ThreadPoolExecutor(max_workers=8) as pool:
                writable = list(pool.map(
                    lambda e: _can_write_tables(client, e['name'], me), pending,
                ))
            for entry, ok in zip(pending, writable):
                if not ok:
                    entry['status'] = 'no_permission'
                    entry['locked'] = True  # not selectable

        # Sort alphabetically; surface Zerobus-eligible first when the
        # flag is on so the UI can match the existing ordering.
        # Locked catalogs sink to the bottom — they are not actionable.
        if _ENABLE_ZEROBUS:
            catalogs.sort(key=lambda x: (
                x['locked'], not x.get('zerobus_eligible', False), x['name'].lower(),
            ))
        else:
            catalogs.sort(key=lambda x: (x['locked'], x['name'].lower()))
        return jsonify({'catalogs': catalogs})
    except RuntimeError as exc:
        msg = str(exc)
        if 'Databricks API error 401' in msg or 'Databricks API error 403' in msg:
            logger.warning('List catalogs — API auth rejected: %s', exc)
            return jsonify({
                'error': 'Databricks session expired — sign in again.',
                'catalogs': [],
                'reauth_required': True,
            }), 401
        logger.error('List catalogs failed: %s', exc)
        return jsonify({'error': msg, 'catalogs': []}), 500
    except Exception as exc:
        logger.error('List catalogs failed: %s', exc)
        return jsonify({'error': str(exc), 'catalogs': []}), 500
