"""
Purpose: The connection HTTP surface (FR-03 §13) — create/resume, Databricks service-principal
credentials, bootstrap, sync, run history and the CDC schedule, plus the internal scheduler tick.

Routes hold no business logic: they authorise via tenant_auth, call a service, and map typed
service exceptions onto status codes. Two rules are enforced here rather than in services:
long work returns 202 and runs on a background thread, and a service-principal secret is
accepted, written straight to the vault, and never echoed back in any response (NFR-01).
"""

from __future__ import annotations

import logging
import os
import threading

from flask import Blueprint, jsonify, request

from backend.repositories.state import (
    connection_repository as conns,
    connection_sync_repository as run_repo,
)
from backend.secrets import get_secret_store
from backend.secrets.secret_store import dbx_sp_secret_ref
from backend.services.m2m import (
    connection_bootstrap,
    connection_runner,
    connection_service,
    dbx_token_service,
    scheduler,
)
from backend.utils import tenant_auth
from backend.utils.databricks_auth import get_valid_dbx_token
from backend.utils.tenant_auth import tenant_route

logger = logging.getLogger(__name__)

connection_bp = Blueprint('connections', __name__)

_DEFAULT_RUN_LIMIT = 50


def _start_thread(fn, *args, **kwargs) -> None:
    """Run work off the request thread. Patched in tests so nothing real is started."""
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def _describe(connection: dict) -> dict:
    """The connection payload every screen renders. Deliberately omits every credential
    field — not even the secret *ref* leaves the server."""
    connection_id = connection['connection_id']
    credentials = conns.get_dbx_credentials(connection_id)
    return {
        'connection': connection,
        'bootstrap': conns.get_bootstrap(connection_id),
        'latest_run': run_repo.latest_run(connection_id),
        'has_service_principal': credentials is not None,
        'dbx_client_id': (credentials or {}).get('client_id'),
        'next_step': connection_service.next_step(connection),
    }


def _store_service_principal(connection: dict, client_id: str, client_secret: str) -> None:
    """Vault the secret, persist only the reference (FR-05's RDS-holds-refs-only rule)."""
    connection_id = connection['connection_id']
    ref = dbx_sp_secret_ref(connection_id)
    get_secret_store().put_secret(ref, client_secret)
    conns.save_dbx_credentials(
        connection_id,
        workspace_url=connection['dbx_workspace_url'],
        client_id=client_id,
        client_secret_ref=ref,
    )
    # A new secret invalidates any token minted from the old one.
    dbx_token_service.invalidate(connection_id)


def _credential_fields(payload: dict, id_key: str, secret_key: str):
    """Both or neither — a client id with no secret would store an unusable principal."""
    client_id = (payload.get(id_key) or '').strip()
    client_secret = (payload.get(secret_key) or '').strip()
    if client_id and not client_secret:
        raise ValueError('A service principal secret is required with the client id.')
    if client_secret and not client_id:
        raise ValueError('A service principal client id is required with the secret.')
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


@connection_bp.route('/api/connections')
@tenant_route
def list_connections():
    _, hub_id = tenant_auth.require_tenant()
    connections = conns.list_for_hub(hub_id)
    return jsonify({
        'connections': connections,
        'next_steps': {
            c['connection_id']: connection_service.next_step(c) for c in connections
        },
    })


@connection_bp.route('/api/connections', methods=['POST'])
@tenant_route
def create_connection():
    """Create or resume. 201 for a new connection, 200 when an existing one was resumed."""
    _, hub_id = tenant_auth.require_tenant()
    payload = request.get_json(silent=True) or {}

    try:
        client_id, client_secret = _credential_fields(
            payload, 'dbx_client_id', 'dbx_client_secret',
        )
        result = connection_service.create_or_resume(
            hub_id=hub_id,
            project_id=(payload.get('project_id') or '').strip(),
            project_name=(payload.get('project_name') or '').strip() or None,
            dbx_workspace_url=(payload.get('workspace_url') or '').strip(),
            catalog=(payload.get('catalog') or '').strip(),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc), 'reason': 'invalid_request'}), 400
    except connection_service.CatalogAlreadyConnected as exc:
        # 409, not 400: the request is well formed, the catalog is simply taken.
        return jsonify({'error': str(exc), 'reason': 'catalog_taken',
                        'catalog': exc.catalog}), 409

    if client_id:
        _store_service_principal(result.connection, client_id, client_secret)

    body = _describe(result.connection) | {'resumed': result.resumed}
    return jsonify(body), 200 if result.resumed else 201


@connection_bp.route('/api/connections/<connection_id>')
@tenant_route
def get_connection(connection_id: str):
    return jsonify(_describe(tenant_auth.require_connection(connection_id)))


@connection_bp.route('/api/connections/<connection_id>/dbx-credentials', methods=['POST'])
@tenant_route
def save_dbx_credentials(connection_id: str):
    connection = tenant_auth.require_connection(connection_id)
    payload = request.get_json(silent=True) or {}
    try:
        client_id, client_secret = _credential_fields(
            payload, 'client_id', 'client_secret',
        )
    except ValueError as exc:
        return jsonify({'error': str(exc), 'reason': 'invalid_request'}), 400
    if not client_id:
        return jsonify({'error': 'A service principal client id and secret are required.',
                        'reason': 'invalid_request'}), 400

    _store_service_principal(connection, client_id, client_secret)
    return jsonify(_describe(conns.get(connection_id)))


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@connection_bp.route('/api/connections/<connection_id>/bootstrap', methods=['POST'])
@tenant_route
def start_bootstrap(connection_id: str):
    """202 — provisioning takes minutes, so it never runs on the request thread."""
    user_id, _ = tenant_auth.require_tenant()
    connection = tenant_auth.require_connection(connection_id)

    user_token = None
    if not conns.has_dbx_credentials(connection_id):
        # D-4: no service principal yet, so borrow the wizard's own Databricks token.
        try:
            user_token = get_valid_dbx_token(user_id)['access_token']
        except Exception:
            return jsonify({
                'error': 'Connect Databricks or add a service principal before '
                         'provisioning this connection.',
                'reason': 'no_databricks_credential',
            }), 400

    if connection_bootstrap.is_in_flight(connection_id):
        return jsonify({'error': 'A bootstrap is already running for this connection.',
                        'reason': 'in_flight'}), 409

    connection_bootstrap.reset_progress(connection_id)
    connection_bootstrap.set_progress(connection_id, 0, 'Starting bootstrap...')
    _start_thread(
        _bootstrap_worker, connection_id, user_token=user_token,
    )
    return jsonify({'started': True, 'connection_id': connection_id}), 202


def _bootstrap_worker(connection_id: str, *, user_token: str | None) -> None:
    """Thread body. Failures are already recorded in progress and on the connection row."""
    try:
        connection_bootstrap.run_connection_bootstrap(
            connection_id, user_token=user_token,
        )
    except Exception as exc:
        logger.exception('Connection bootstrap failed for %s: %s', connection_id, exc)


@connection_bp.route('/api/connections/<connection_id>/bootstrap/status')
@tenant_route
def bootstrap_status(connection_id: str):
    connection = tenant_auth.require_connection(connection_id)
    progress = connection_bootstrap.get_progress(connection_id)
    progress['onboarding_status'] = connection['onboarding_status']
    progress['last_error'] = connection['last_error']
    return jsonify(progress)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _start_sync(connection_id: str, mode: str):
    user_id, _ = tenant_auth.require_tenant()
    connection = tenant_auth.require_connection(connection_id)

    in_flight = run_repo.has_in_flight_run(connection_id)
    if in_flight:
        return jsonify({
            'error': f'A {in_flight["mode"]} sync is already running for this connection.',
            'reason': 'in_flight', 'run_id': in_flight['run_id'],
        }), 409

    user_token = None
    if not conns.has_dbx_credentials(connection_id):
        try:
            user_token = get_valid_dbx_token(user_id)['access_token']
        except Exception:
            user_token = None

    # Fail fast on anything the runner would reject anyway, so the UI gets a real message
    # instead of a 202 followed by a silent failure.
    try:
        connection_runner.assert_ready(
            connection, conns.get_bootstrap(connection_id), mode,
        )
    except connection_runner.SyncNotReady as exc:
        return jsonify({'error': str(exc), 'reason': 'not_ready'}), 400

    _start_thread(
        _sync_worker, connection_id, mode=mode, trigger_type='manual',
        user_token=user_token,
    )
    return jsonify({'started': True, 'mode': mode}), 202


def _sync_worker(connection_id: str, *, mode: str, trigger_type: str,
                 user_token: str | None) -> None:
    try:
        connection_runner.run_connection_sync(
            connection_id, mode=mode, trigger_type=trigger_type, user_token=user_token,
        )
    except Exception as exc:
        logger.exception('Connection sync failed for %s: %s', connection_id, exc)


@connection_bp.route('/api/connections/<connection_id>/sync', methods=['POST'])
@tenant_route
def start_snapshot_sync(connection_id: str):
    return _start_sync(connection_id, 'snapshot')


@connection_bp.route('/api/connections/<connection_id>/sync/cdc', methods=['POST'])
@tenant_route
def start_cdc_sync(connection_id: str):
    return _start_sync(connection_id, 'cdc')


@connection_bp.route('/api/connections/<connection_id>/sync/status')
@tenant_route
def sync_status(connection_id: str):
    tenant_auth.require_connection(connection_id)
    latest = run_repo.latest_run(connection_id)
    return jsonify({
        'run': latest,
        'in_flight': run_repo.has_in_flight_run(connection_id) is not None,
    })


@connection_bp.route('/api/connections/<connection_id>/runs')
@tenant_route
def list_runs(connection_id: str):
    tenant_auth.require_connection(connection_id)
    try:
        limit = int(request.args.get('limit', _DEFAULT_RUN_LIMIT))
    except ValueError:
        limit = _DEFAULT_RUN_LIMIT
    return jsonify({'runs': run_repo.list_runs(connection_id, max(1, min(limit, 200)))})


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@connection_bp.route('/api/connections/<connection_id>/schedule', methods=['PUT'])
@tenant_route
def set_schedule(connection_id: str):
    """Toggle headless CDC. A refusal carries the one action that would fix it (D-18)."""
    tenant_auth.require_connection(connection_id)
    payload = request.get_json(silent=True) or {}
    enabled = payload.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'error': 'enabled must be true or false.',
                        'reason': 'invalid_request'}), 400

    if not enabled:
        connection_service.disable_cdc(connection_id)
        return jsonify(_describe(conns.get(connection_id)))

    try:
        connection_service.enable_cdc(connection_id)
    except connection_service.SchedulingBlocked as exc:
        return jsonify({'error': exc.message, 'reason': exc.reason,
                        'remediation': exc.remediation}), 409
    return jsonify(_describe(conns.get(connection_id)))


# ---------------------------------------------------------------------------
# Internal scheduler tick — OS cron / EventBridge, never a browser
# ---------------------------------------------------------------------------


@connection_bp.route('/internal/scheduler/cdc', methods=['POST'])
def scheduler_tick():
    """Shared-secret authenticated, session-free (FR-03 §13).

    Fails closed when the token is unset: an unconfigured deployment must not expose an
    unauthenticated endpoint that can start work in every tenant.
    """
    expected = os.getenv('INTERNAL_SCHEDULER_TOKEN', '').strip()
    if not expected:
        return jsonify({'error': 'Scheduler endpoint is not configured.',
                        'reason': 'not_configured'}), 503
    if request.headers.get('X-Internal-Token', '') != expected:
        return jsonify({'error': 'Unauthorized.', 'reason': 'bad_token'}), 401
    return jsonify(scheduler.scheduled_cdc_tick())
