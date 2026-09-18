
import logging
import os
import threading
from flask import Flask, abort, jsonify, request, session
from backend.repositories import state_store as db
from backend.services import bootstrap_service as bs

_ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
if _ENABLE_ZEROBUS:
    # from backend import zerobus_status
    from backend.services import zerobus_service
else:
    zerobus_service= None  # sentinel — guards every site that referenced the module
    # zerobus_status = None  # sentinel — guards every site that referenced the module


logger = logging.getLogger(__name__)

from flask import Blueprint

bootstrap_bp = Blueprint(
    "bootstrap",
    __name__
)

from backend.utils.auth import _current_user_id, _require_user_id
from backend.utils.databricks_auth import get_valid_dbx_token

# ------------------------------------------------------------------
# Databricks OIDC connection + bootstrap (Azure and AWS)
# ------------------------------------------------------------------
@bootstrap_bp.route('/bootstrap/start', methods=['POST'])
def bootstrap_start():
    """Start workspace provisioning for the selected Unity Catalog (background thread)."""
    uid = _require_user_id()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'Expected JSON body'}), 400
    raw_name = payload.get('catalog_name', '')
    try:
        catalog_name = bs.validate_uc_catalog_name(raw_name)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    try:
       token = get_valid_dbx_token(uid)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 401

    if not bs.try_begin_bootstrap(uid):
        return jsonify({'error': 'Bootstrap already in progress for this user.'}), 409

    # Claim here, not in the worker thread — the background bootstrap has no way
    # to report a conflict back to the browser, so the lock must be resolved
    # while we can still answer 409.
    try:
        claim = db.claim_catalog(
            db.normalize_workspace_key(token['workspace_url']), catalog_name, uid,
        )
    except db.CatalogLockedError as exc:
        bs.end_bootstrap(uid)
        return jsonify({'error': str(exc)}), 409

    claim_was_unprovisioned = not claim.get('snapshot_pipeline_id')
    bs._set_progress(uid, 0, 'Starting bootstrap...')

    def _run():
        try:
            fresh = get_valid_dbx_token(uid)
            bs.run_bootstrap(
                uid, fresh['workspace_url'], fresh['access_token'], catalog_name,
                claim_id=claim['claim_id'],
            )
        except Exception as exc:
            logger.error('Background bootstrap error: %s', exc)
            bs.fail_bootstrap(uid, f'Bootstrap failed: {exc}')
            if claim_was_unprovisioned:
                db.release_claim(claim['claim_id'])
        finally:
            bs.end_bootstrap(uid)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({
        'started': True,
        'claim_id': claim['claim_id'],
    }), 202


@bootstrap_bp.route('/bootstrap/status')
def bootstrap_status():
    uid = _require_user_id()
    progress = bs.get_progress(uid)
    return jsonify(progress)
