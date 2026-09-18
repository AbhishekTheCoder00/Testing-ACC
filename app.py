"""
ACC → Databricks Connector v2
Flask application — all routes and UI.

User flow:
  1. Connect ACC  (3-legged OAuth → hub/project selection)
  2. Connect Databricks  (Databricks OIDC → pick Unity Catalog → 10-step provisioning)
  3. Sync  (ACC API fetch → UC Volume → Bronze AUTO CDC pipeline)
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from backend.clients import acc_client
from backend.repositories import state_store as db
from backend.services import bootstrap_service as bs
from backend.clients.databricks_client import DatabricksClient


# # Zerobus prerequisite diagnostics only (not a sync writer). Loaded when
# # ENABLE_ZEROBUS=true; conditional import so flag-off deployments skip it.
# _ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
# if _ENABLE_ZEROBUS:
#     # from backend import zerobus_status
#     from backend.services import zerobus_service
# else:
#     zerobus_service= None  # sentinel — guards every site that referenced the module
#     # zerobus_status = None  # sentinel — guards every site that referenced the module

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ### Register blueprints
# Blueprints for route organization — see backend/routes/*.py. Each blueprint imports the shared db and clients as needed, but they all share the same Flask session and app context.
from backend.routes.bootstrap_routes import bootstrap_bp
from backend.routes.sync_routes import sync_bp
from backend.routes.databricks_routes import databricks_bp
from backend.routes.acc_routes import acc_bp
from backend.routes.dashboard_routes import dashboard_bp
# Multi-tenant M2M portal (Phase 1). A separate page and API surface from the U2M wizard
# above; the two share only the ACC OAuth handshake in acc_routes.
from backend.routes.portal_routes import portal_bp, POST_LOGIN_KEY, PORTAL_PATH
from backend.routes.onboarding_routes import onboarding_bp
from backend.routes.connection_routes import connection_bp

app.register_blueprint(acc_bp)
app.register_blueprint(bootstrap_bp)
app.register_blueprint(sync_bp)
app.register_blueprint(databricks_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(portal_bp)
app.register_blueprint(onboarding_bp)
app.register_blueprint(connection_bp)

_secret_key = os.getenv('SECRET_KEY')
if not _secret_key:
    raise RuntimeError('SECRET_KEY environment variable must be set')
app.secret_key = _secret_key

# Multi-tenant session: identity is derived at /callback from the APS userinfo
# response and stored in session['user_id']. The signed Flask cookie keeps
# concurrent pilot users isolated without a separate user-account table.
# 1-day rolling idle timeout — every authenticated request resets the clock,
# so a user who keeps using the connector stays signed in; an idle browser
# is logged out after 24 hours. Tokens themselves still refresh on their own
# ACC/Databricks expiry timers.
app.permanent_session_lifetime = timedelta(days=1)
from backend.utils.auth import _current_user_id, _require_user_id

db.init_db()


# ---------------------------------------------------------------------------
# U2M on/off switch
# ---------------------------------------------------------------------------
# ENABLE_U2M=false turns the M2M portal into the site's front door and takes the U2M
# wizard off the air. Read per request rather than cached at import: env is the only
# source available here (config.get_config falls back to a per-user DB row, and both the
# landing page and the guard below run before anyone is signed in).
def _u2m_enabled() -> bool:
    return os.getenv('ENABLE_U2M', 'true').lower() == 'true'


# The portal owns three blueprints, but it also borrows four routes from the U2M
# blueprints — ACC sign-in, the Databricks OAuth pair, the project picker and the catalog
# picker. So the block is an endpoint allowlist, not a per-blueprint switch: dropping
# acc_bp/databricks_bp wholesale would break portal sign-in and the connection wizard.
_M2M_BLUEPRINTS = {'portal', 'onboarding', 'connections'}
_M2M_ENDPOINTS = {
    'static', 'index', 'health',
    'acc.connect_acc', 'acc.oauth_callback', 'acc.get_projects',
    'databricks.connect_databricks_oauth', 'databricks.databricks_callback',
    'databricks.databricks_catalogs',
}


@app.before_request
def _block_u2m_routes():
    """404 every wizard-only route when U2M is disabled. 404, not 403: on such a
    deployment the wizard does not exist."""
    if _u2m_enabled():
        return None
    if request.blueprint in _M2M_BLUEPRINTS or request.endpoint in _M2M_ENDPOINTS:
        return None
    abort(404)


from backend.routes.sync_routes import rehydrate_auto_sync_timers
# Blocking the wizard's routes while its daily CDC timers keep firing would leave U2M
# syncs running with no UI to stop them, so the timers follow the same switch.
if _u2m_enabled():
    rehydrate_auto_sync_timers()

# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# UI templates
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    # A portal sign-in comes back through the shared ACC callback, which lands here. Honour
    # the flag the portal set so the user returns to /portal instead of the U2M wizard. Only
    # the portal ever sets it, so the U2M flow is unchanged.
    destination = session.pop(POST_LOGIN_KEY, None)
    if destination:
        return redirect(destination)
    if not _u2m_enabled():
        return redirect(PORTAL_PATH)
    return render_template(
        'index.html',
        connector_docs_url=os.getenv('CONNECTOR_DOCS_URL', '').strip(),
    )
#///////////////////////////////////////////////

# ------------------------------------------------------------------
# Databricks OIDC connection + bootstrap (Azure and AWS)
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# Data Connector request management
# ------------------------------------------------------------------

@app.route('/dc/requests')
def list_dc_requests():
    uid = _require_user_id()
    try:
        token = acc_client.get_valid_token(uid)
        cfg   = db.get_acc_config(uid) or {}
        hub_id     = cfg.get('hub_id', '')
        account_id = cfg.get('acc_account_id') or (hub_id[2:] if hub_id.startswith('b.') else hub_id)
        project_id = cfg.get('project_id', '')
        bare_pid   = project_id[2:] if project_id.startswith('b.') else project_id
        reqs = acc_client.dc_list_requests(token, account_id, project_id=bare_pid)
        return jsonify({'requests': reqs})
    except Exception as e:
        logger.error('list_dc_requests error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/dc/requests/<request_id>', methods=['DELETE'])
def delete_dc_request(request_id):
    uid = _require_user_id()
    try:
        token = acc_client.get_valid_token(uid)
        cfg   = db.get_acc_config(uid) or {}
        hub_id     = cfg.get('hub_id', '')
        account_id = cfg.get('acc_account_id') or (hub_id[2:] if hub_id.startswith('b.') else hub_id)
        acc_client.dc_delete_request(token, account_id, request_id)
        return jsonify({'message': f'Request {request_id} deleted'}), 200
    except Exception as e:
        logger.error('delete_dc_request error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
