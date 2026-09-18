
import logging
import os
from flask import Flask, abort, jsonify, redirect,request, session

from backend.clients import acc_client
from backend.clients.databricks_client import DatabricksClient
from backend.utils.databricks_auth import get_valid_dbx_token
from backend.services.sync.pipeline_config import sync_project_pipeline_defaults

from backend.repositories import state_store as db

from flask import Blueprint

acc_bp = Blueprint(
    "acc",
    __name__
)

_ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
if _ENABLE_ZEROBUS:
    # from backend import zerobus_status
    from backend.services import zerobus_service
else:
    zerobus_service= None  # sentinel — guards every site that referenced the module
    # zerobus_status = None  # sentinel — guards every site that referenced the module


logger = logging.getLogger(__name__)

from backend.utils.auth import _current_user_id, _require_user_id
from backend.routes.sync_routes import stop_all_auto_sync_for_user

@acc_bp.route('/state')

def state():
    """Return current connection state for the UI to render on load.

    If no user is authenticated yet, every flag is False — the UI then renders
    the "Connect ACC" entry point with no leakage of any other tenant's state.
    """
    uid = _current_user_id()
    if not uid:
        return jsonify({
            'acc_token_present':  False,
            'acc_connected':      False,
            'project_name':       None,
            'hub_name':           None,
            'dbx_token_present':  False,
            'dbx_bootstrapped':   False,
            'has_sync_history':   False,
            'dbx_workspace_url':  None,
            'dbx_cloud_provider': None,
        })

    tokens     = db.get_acc_tokens(uid)
    acc_cfg    = db.get_acc_config(uid)
    bs_state   = db.get_bootstrap_state(uid)
    dbx_tokens = db.get_dbx_tokens(uid)
    # Steps 4–5 unlock after a successful Sync Snapshot baseline, not when the
    # latest run happens to be terminal — an in-progress CDC run must not
    # re-lock Auto CDC and Dashboard on page refresh.
    has_history = bool(
        acc_cfg
        and db.has_successful_snapshot(uid, acc_cfg['project_id'])
    )
    return jsonify({
        'acc_token_present': tokens is not None,
        'acc_connected':     acc_cfg is not None,
        'project_name':      acc_cfg['project_name'] if acc_cfg else None,
        'hub_name':          acc_cfg['hub_name'] if acc_cfg else None,
        'dbx_token_present': dbx_tokens is not None,
        'dbx_bootstrapped':  bs_state is not None and bs_state.get('snapshot_pipeline_id') is not None,
        'has_sync_history':  has_history,
        'dbx_workspace_url': dbx_tokens['workspace_url'] if dbx_tokens else None,
        'dbx_cloud_provider': dbx_tokens['cloud_provider'] if dbx_tokens else None,
    })


# ------------------------------------------------------------------
# ACC OAuth
# ------------------------------------------------------------------

# @app.route('/connect/acc')
# def connect_acc():
#     return redirect(acc_client.get_auth_url())

@acc_bp.route('/connect/acc')
def connect_acc():
    return redirect(acc_client.get_auth_url())


@acc_bp.route('/callback')
def oauth_callback():
    code  = request.args.get('code')
    error = request.args.get('error')
    if error:
        return f'<p>ACC authentication error: {error}</p><p><a href="/">Go back</a></p>', 400
    if not code:
        return '<p>No authorization code received.</p><p><a href="/">Go back</a></p>', 400
    try:
        # handle_callback returns the Autodesk userId after a successful userinfo
        # lookup. Binding it to the Flask session is what isolates concurrent
        # pilot users — every subsequent request reads its user_id from here.
        user_id = acc_client.handle_callback(code)
        # Intentionally NOT session.permanent = True. We want the
        # signed cookie to be a browser-session cookie (no Max-Age /
        # Expires header) so closing the browser drops the session and
        # the next visit lands on the login screen. Persistent
        # ACC/Databricks tokens still live in connector.db keyed by
        # user_id; logging back in with the same Autodesk account
        # rebinds them to a new session.
        session['user_id'] = user_id
        logger.info('ACC OAuth callback complete for user %s', user_id)
    except Exception as e:
        logger.error('OAuth callback error: %s', e)
        return f'<p>OAuth error: {e}</p><p><a href="/">Go back</a></p>', 500
    return redirect('/?fresh=1')


@acc_bp.route('/logout', methods=['GET', 'POST'])
def logout():
    """Clear the session so a different user can sign in from the same browser.

    Persisted ACC/Databricks tokens stay in SQLite — re-authenticating with the
    same Autodesk account restores access to that tenant's state.
    """
    session.clear()
    return redirect('/')

@acc_bp.route('/reset', methods=['GET', 'POST'])
def reset():
    """Fresh start for the current user: clear session and delete their DB rows only."""
    uid = _current_user_id()
    session.clear()
    if uid:
        try:
            # Ensure connector.db and tables exist (no-op when already initialized).
            # App startup also calls init_db(); this covers edge cases such as the
            # DB file being removed while the process is still running.
            db.init_db()
            stop_all_auto_sync_for_user(uid)
            db.reset_user(uid)
            db.clear_credentials(uid)
            logger.info('Reset: cleared persisted state for user %s', uid)
        except Exception as e:
            logger.error('Reset: could not clear state for user %s: %s', uid, e)
            return (
                '<p>Could not clear your saved state. Please try again.</p>'
                '<p><a href="/">Go back</a></p>'
            ), 503
    return redirect('/?fresh=1')


@acc_bp.route('/hubs')
def get_hubs():
    uid = _require_user_id()
    try:
        hubs = acc_client.fetch_hubs(uid)
        return jsonify({'hubs': hubs})
    except Exception as e:
        logger.error('get_hubs error: %s', e)
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/projects')
def get_projects():
    uid = _require_user_id()
    hub_id = request.args.get('hub_id', '').strip()
    if not hub_id:
        return jsonify({'error': 'hub_id is required'}), 400
    try:
        projects = acc_client.fetch_projects(uid, hub_id)
        return jsonify({'projects': projects})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/debug/acc')
def debug_acc():
    """Dev-only: returns current token + config for Postman testing. Remove before prod."""
    uid = _require_user_id()
    try:
        token   = acc_client.get_valid_token(uid)   # auto-refreshes if near expiry
        cfg     = db.get_acc_config(uid) or {}
        hub_id  = cfg.get('hub_id', '')
        account_id = cfg.get('acc_account_id') or (hub_id[2:] if hub_id.startswith('b.') else hub_id)
        project_id = cfg.get('project_id', '')
        bare_pid   = project_id[2:] if project_id.startswith('b.') else project_id
        return jsonify({
            'bearer_token': token,
            'account_id':   account_id,
            'project_id':   bare_pid,
            'project_name': cfg.get('project_name', ''),
            'hub_name':     cfg.get('hub_name', ''),
            'region':       'US',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/debug/hubs')
def debug_hubs():
    """Diagnostic: returns raw hub lists from both 2-legged and 3-legged tokens."""
    uid = _require_user_id()
    try:
        two_leg   = acc_client._get_2legged_token()
        three_leg = acc_client.get_valid_token(uid)
        two_leg_resp   = acc_client._get(f'{acc_client.APS_BASE_DM}/hubs', two_leg)
        three_leg_resp = acc_client._get(f'{acc_client.APS_BASE_DM}/hubs', three_leg)
        merged = acc_client.fetch_hubs(uid)
        return jsonify({
            'two_legged_count':   len(two_leg_resp.get('data', [])),
            'three_legged_count': len(three_leg_resp.get('data', [])),
            'merged_count':       len(merged),
            'two_legged_hubs':   [
                {'id': h['id'], 'name': h['attributes']['name']}
                for h in two_leg_resp.get('data', [])
            ],
            'three_legged_hubs': [
                {'id': h['id'], 'name': h['attributes']['name']}
                for h in three_leg_resp.get('data', [])
            ],
            'merged_hubs': merged,
        })
    except Exception as e:
        logger.error('debug_hubs error: %s', e)
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/admin-projects')
def get_admin_projects():
    """Return flat list of all projects the logged-in user has access to."""
    uid = _require_user_id()
    try:
        projects = acc_client.fetch_user_projects(uid)
        return jsonify({'projects': projects})
    except Exception as e:
        logger.error('get_admin_projects error: %s', e)
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/folders')
def get_folders():
    uid = _require_user_id()
    hub_id     = request.args.get('hub_id', '').strip()
    project_id = request.args.get('project_id', '').strip()
    if not hub_id or not project_id:
        return jsonify({'error': 'hub_id and project_id are required'}), 400
    try:
        folders = acc_client.fetch_top_folders(uid, hub_id, project_id)
        return jsonify({'folders': folders})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@acc_bp.route('/save-acc-config', methods=['POST'])
def save_acc_config():
    uid = _require_user_id()
    data = request.json or {}
    required = ('hub_id', 'hub_name', 'project_id', 'project_name')
    if not all(data.get(k) for k in required):
        return jsonify({'error': 'hub_id, hub_name, project_id, project_name are required'}), 400
    db.save_acc_config(
        user_id=uid,
        hub_id=data['hub_id'],
        hub_name=data['hub_name'],
        project_id=data['project_id'],
        project_name=data['project_name'],
        folder_id=data.get('folder_id'),
        folder_name=data.get('folder_name'),
        acc_account_id=data.get('acc_account_id'),
    )

    bare_project_id = data['project_id']
    if bare_project_id.startswith('b.'):
        bare_project_id = bare_project_id[2:]

    bs_state = db.get_bootstrap_state(uid)
    if bs_state and bs_state.get('snapshot_pipeline_id'):
        try:
            dbx_tok = get_valid_dbx_token(uid)
            dbx = DatabricksClient(
                dbx_tok['workspace_url'],
                dbx_tok['access_token'],
            )
            sync_project_pipeline_defaults(dbx, bs_state, bare_project_id)
        except Exception as exc:
            logger.warning(
                'ACC config saved but pipeline default configuration sync failed: %s',
                exc,
            )

    return jsonify({'message': f'ACC config saved — project: {data["project_name"]}'}), 200

