"""
Purpose: Bootstraps one connection's Databricks workspace by *driving* the shared step library
rather than re-implementing it (D-1/D-2) — the U2M driver in bootstrap_service does the same
over the same steps. This module owns only what differs per path: which Databricks token
(service principal first, the wizard's user token as the D-4 fallback), a connection-keyed
progress sink the wizard polls, the catalog claim, and persistence to connection_bootstrap.
"""

from __future__ import annotations

import logging
import threading

from backend.clients.databricks_client import DatabricksClient
from backend.repositories.state import connection_repository as conns
from backend.services.m2m import connection_service, dbx_token_service
from backend.services.provisioning import bootstrap_steps

logger = logging.getLogger(__name__)

# Step 11 is the driver's own step number — the shared library stops at 10c, exactly as the
# U2M driver leaves it.
_PERSIST_STEP = 11

_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


class UnknownConnection(Exception):
    """No such connection — a tampered or stale id."""


class BootstrapNotAuthorised(Exception):
    """Neither a service principal nor a wizard token is available for this connection."""


class BootstrapInFlight(Exception):
    """A bootstrap is already running for this connection."""


# ---------------------------------------------------------------------------
# Progress — keyed by connection_id, polled by the wizard
# ---------------------------------------------------------------------------


def set_progress(connection_id: str, step: int, message: str, *,
                 done: bool = False, error: str | None = None) -> None:
    with _progress_lock:
        _progress[connection_id] = {
            'step': step,
            'message': message,
            'done': done or bool(error),
            'error': error,
        }
    if error:
        logger.error('Bootstrap step %d failed for %s: %s', step, connection_id, error)
    else:
        logger.info('Bootstrap step %d [%s]: %s', step, connection_id, message)


def get_progress(connection_id: str) -> dict:
    with _progress_lock:
        return dict(
            _progress.get(connection_id)
            or {'step': 0, 'message': '', 'done': False, 'error': None}
        )


def reset_progress(connection_id: str) -> None:
    with _progress_lock:
        _progress.pop(connection_id, None)


def try_begin(connection_id: str) -> bool:
    """Claim the in-flight slot. False means one is already running (the wizard polls, and a
    double-click must not start a second provisioning run)."""
    with _in_flight_lock:
        if connection_id in _in_flight:
            return False
        _in_flight.add(connection_id)
        return True


def end(connection_id: str) -> None:
    with _in_flight_lock:
        _in_flight.discard(connection_id)


def is_in_flight(connection_id: str) -> bool:
    """Read-only check for routes. The authoritative guard is try_begin inside
    run_connection_bootstrap — this only lets a route answer 409 without claiming the slot."""
    with _in_flight_lock:
        return connection_id in _in_flight


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def _resolve_token(connection_id: str, user_token: str | None) -> str:
    """Service principal first, the wizard's own Databricks token second (D-4).

    Scheduled runs are gated separately on the SP existing, so falling back here only ever
    affects work a human is watching.
    """
    if conns.has_dbx_credentials(connection_id):
        return dbx_token_service.get_dbx_token(connection_id)
    if user_token:
        return user_token
    raise BootstrapNotAuthorised(
        'This connection has no Databricks credentials. Add a service principal, or '
        'reconnect Databricks and try again.'
    )


def run_connection_bootstrap(connection_id: str, *, user_token: str | None = None) -> dict:
    """Provision this connection's catalog. Raises on failure; the route runs it on a thread.

    The claim is re-taken before any Databricks call so a catalog that changed hands while the
    wizard sat open stops the run instead of provisioning into someone else's catalog.
    """
    connection = conns.get(connection_id)
    if not connection:
        raise UnknownConnection(connection_id)

    if not try_begin(connection_id):
        raise BootstrapInFlight(connection_id)

    try:
        claim = connection_service.claim_catalog(connection)
        token = _resolve_token(connection_id, user_token)
        catalog_name = connection['catalog']

        def progress(step: int, message: str, *, error: str | None = None) -> None:
            set_progress(connection_id, step, message, error=error)

        dbx = DatabricksClient(connection['dbx_workspace_url'], token)
        try:
            result = bootstrap_steps.run_all(
                dbx, catalog_name, progress=progress, create_workflows=True,
            )
        except Exception as exc:
            # Leave the connection exactly where the wizard can retry it, with the reason.
            conns.update_status(connection_id, 'pending_bootstrap', last_error=str(exc))
            set_progress(connection_id, get_progress(connection_id)['step'], '',
                         error=str(exc))
            raise

        set_progress(connection_id, _PERSIST_STEP, 'Saving bootstrap configuration...')
        out = _persist(connection, dbx, claim, result)
        set_progress(connection_id, _PERSIST_STEP, 'Bootstrap complete!', done=True)
        return out
    finally:
        end(connection_id)


def _persist(connection: dict, dbx: DatabricksClient, claim: dict, result: dict) -> dict:
    """Store the artefact ids and push this project's defaults onto the pipelines.

    Connection-keyed rows are the one genuinely path-specific part of bootstrap, which is why
    this lives in the driver and not in the step library.
    """
    connection_id = connection['connection_id']

    conns.save_bootstrap(
        connection_id,
        notebook_folder=result['notebook_folder'],
        volume_path=result['volume_path'],
        warehouse_id=result['warehouse_id'],
        catalog_name=result['catalog_name'],
        snapshot_workflow_id=result['snapshot_workflow_id'],
        cdc_workflow_id=result['cdc_workflow_id'],
        catalog_claim_id=claim['claim_id'],
    )
    conns.set_pipeline_ids(
        connection_id,
        snapshot_pipeline_id=result['snapshot_pipeline_id'],
        cdc_pipeline_id=result['cdc_pipeline_id'],
    )

    names = result['pipeline_names']
    from backend.repositories.state import catalog_claim_repository as claim_repo

    claim_repo.update_claim_artifacts(
        claim['claim_id'],
        snapshot_pipeline_id=result['snapshot_pipeline_id'],
        cdc_pipeline_id=result['cdc_pipeline_id'],
        snapshot_pipeline_name=names['snapshot_pipeline'],
        cdc_pipeline_name=names['cdc_pipeline'],
        snapshot_workflow_id=result['snapshot_workflow_id'],
        cdc_workflow_id=result['cdc_workflow_id'],
    )

    _push_pipeline_defaults(dbx, connection, result)
    connection_service.mark_ready(connection_id)

    out = dict(result)
    out['catalog_claim_id'] = claim['claim_id']
    out['connection_id'] = connection_id
    return out


def _push_pipeline_defaults(dbx: DatabricksClient, connection: dict, result: dict) -> None:
    """Best-effort, exactly as U2M treats it — a failure here does not undo a good bootstrap."""
    project_id = connection.get('project_id') or ''
    if not project_id:
        return
    bare_pid = project_id[2:] if project_id.startswith('b.') else project_id
    try:
        from backend.services.sync.pipeline_config import sync_project_pipeline_defaults

        sync_project_pipeline_defaults(
            dbx,
            {
                'catalog_name': result['catalog_name'],
                'snapshot_pipeline_id': result['snapshot_pipeline_id'],
                'cdc_pipeline_id': result['cdc_pipeline_id'],
            },
            bare_pid,
        )
    except Exception as exc:
        logger.warning('Connection bootstrap: pipeline default config sync failed: %s', exc)
