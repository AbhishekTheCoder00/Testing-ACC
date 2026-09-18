"""
Purpose: Runs one connection's sync headlessly by driving the shared sync steps (D-1/D-2)
instead of re-implementing the export sequence. This driver owns the parts that make a run
headless: the hub service account as the ACC TokenGetter (BR-08 — no acc_tokens row is read),
the connection's Databricks service principal, and a SyncPorts adapter that maps the shared
run-state vocabulary onto the connection-keyed tables and their FR-05 §10.3 states.
"""

from __future__ import annotations

import logging

from backend.clients.databricks_client import DatabricksClient
from backend.repositories.state import (
    connection_repository as conns,
    connection_sync_repository as runs,
)
from backend.services.m2m import dbx_token_service, provisioning_probe, ssa_token_service
from backend.services.sync import sync_steps
from backend.services.sync.watermark_service import (
    iso_window_from_timestamp,
    watermark_keys_for,
)

logger = logging.getLogger(__name__)

# The shared steps speak the U2M vocabulary (see sync_steps.SyncPorts); connection_sync_runs
# stores the FR-05 §10.3 set. Mapping here, in the adapter, keeps one phase sequence in the
# shared code instead of two.
_STATE_MAP = {
    'running': 'pending',
    'dc_job_submitted': 'exporting',
    'uploading_files': 'downloading',
    'bronze_written': 'downloading',
    'workflow_triggered': 'pipeline_running',
    'workflow_running': 'pipeline_running',
    'bronze_job_triggered': 'pipeline_running',
    'bronze_job_running': 'pipeline_running',
    'complete': 'success',
    'failed': 'failed',
}

_RUN_FIELDS = frozenset({'record_counts', 'bronze_run_id', 'error', 'phase', 'files_count'})

# Substrings that mean "Autodesk refused us", which is the signal to re-check whether the
# robot is still invited to the project (D-17) rather than leaving a stale green tick.
_PERMISSION_MARKERS = ('401', '403', 'forbidden', 'unauthorized', 'not authorized')


class UnknownConnection(Exception):
    """No such connection."""


class SyncNotReady(Exception):
    """The connection is not bootstrapped enough to sync, and the message says why."""


class SyncNotAuthorised(Exception):
    """No usable Databricks credential for this run."""


class SyncInFlight(Exception):
    """This connection already has a run going (FR-03 §12.2)."""

    def __init__(self, run: dict):
        super().__init__(
            f'A {run["mode"]} sync is already running for this connection '
            f'(run {run["run_id"]}).'
        )
        self.run = run


def _resolve_token(connection_id: str, trigger_type: str, user_token: str | None) -> str:
    """Service principal first; a human's token is acceptable only for manual runs (D-4).

    A scheduled tick that borrowed a user token would break BR-08's promise that headless
    sync never depends on someone being logged in.
    """
    if conns.has_dbx_credentials(connection_id):
        return dbx_token_service.get_dbx_token(connection_id)
    if trigger_type != 'manual':
        raise SyncNotAuthorised(
            'Scheduled sync needs a Databricks service principal for this connection.'
        )
    if user_token:
        return user_token
    raise SyncNotAuthorised(
        'This connection has no Databricks credentials. Add a service principal, or '
        'reconnect Databricks and try again.'
    )


def build_ports(connection: dict, dbx: DatabricksClient) -> sync_steps.SyncPorts:
    """Bind the shared steps to this connection's tables and machine credentials."""
    connection_id = connection['connection_id']
    hub_id = connection['hub_id']
    get_acc_token = ssa_token_service.token_getter(hub_id)

    def update_run(run_id, state, **kwargs):
        mapped = _STATE_MAP.get(state)
        if mapped is None:
            raise ValueError(f'unknown shared sync state: {state!r}')
        runs.update_run(
            run_id, mapped,
            **{k: v for k, v in kwargs.items() if k in _RUN_FIELDS},
        )

    def resolve_window(watermark_key):
        return _resolve_window(connection_id, watermark_key)

    def commit_watermarks(mode, trigger_type):
        keys = watermark_keys_for(mode, trigger_type)
        for key in keys:
            runs.set_watermark(connection_id, data_type=key)
        logger.info(
            '[sync-watermark] committed mode=%s trigger=%s connection=%s keys=%s',
            mode, trigger_type, connection_id, ','.join(keys) or mode,
        )

    def refresh_dbx():
        dbx.update_token(dbx_token_service.get_dbx_token(connection_id, force=True))

    return sync_steps.SyncPorts(
        get_acc_token=get_acc_token,
        refresh_dbx=refresh_dbx,
        create_run=lambda mode, trigger, watermark_key: runs.create_run(
            connection_id, mode=mode, trigger_type=trigger,
        ),
        update_run=update_run,
        resolve_window=resolve_window,
        commit_watermarks=commit_watermarks,
    )


def _resolve_window(connection_id: str, watermark_key: str) -> tuple[str | None, str | None]:
    """The DC export window, anchored on the last *successful* run for this connection.

    Anchoring on success rather than the last attempt is what stops a failed retry from
    shrinking the next window (FR-03 §18 Q7); the stored watermark is the fallback for a
    connection whose run history has been trimmed.
    """
    ts = runs.last_successful_timestamp(connection_id, watermark_key)
    source = 'successful_run'
    if ts is None:
        ts = runs.get_watermark(connection_id, watermark_key)
        source = 'watermark' if ts is not None else 'none'

    logger.info(
        '[sync-window] connection=%s key=%s source=%s', connection_id, watermark_key, source,
    )
    if ts is None:
        return None, None
    return iso_window_from_timestamp(ts)


def _build_target(connection: dict, bootstrap: dict) -> sync_steps.SyncTarget:
    hub_id = connection['hub_id']
    project_id = connection['project_id']
    return sync_steps.SyncTarget(
        # The ACC account id is the de-prefixed hub id and is used only in the Data
        # Connector URL path — the tenant boundary itself stays hub_id (FR-01).
        account_id=hub_id[2:] if hub_id.startswith('b.') else hub_id,
        project_id=project_id,
        bare_project_id=project_id[2:] if project_id.startswith('b.') else project_id,
        catalog_name=bootstrap.get('catalog_name') or connection['catalog'],
        volume_base=bootstrap.get('volume_path') or '',
        snapshot_pipeline_id=connection.get('snapshot_pipeline_id'),
        cdc_pipeline_id=connection.get('cdc_pipeline_id'),
        snapshot_workflow_id=bootstrap.get('snapshot_workflow_id'),
        cdc_workflow_id=bootstrap.get('cdc_workflow_id'),
        warehouse_id=bootstrap.get('warehouse_id'),
    )


def assert_ready(connection: dict, bootstrap: dict | None, mode: str) -> dict:
    if connection['onboarding_status'] != 'ready':
        raise SyncNotReady(
            'This connection is not bootstrapped yet — finish setup before syncing.'
        )
    if not bootstrap:
        raise SyncNotReady('Bootstrap not complete — run workspace bootstrap first.')
    if mode == 'snapshot' and not connection.get('snapshot_pipeline_id'):
        raise SyncNotReady(
            'Snapshot pipeline not provisioned — re-run bootstrap for this connection.'
        )
    if mode == 'cdc' and not connection.get('cdc_pipeline_id'):
        raise SyncNotReady(
            'CDC pipeline not provisioned — re-run bootstrap for this connection.'
        )
    return bootstrap


def _reverify_on_permission_error(connection: dict, message: str) -> None:
    """Re-ask Autodesk whether the robot is still on the project (D-17).

    Without this a robot removed from a project after setup would stay green in our own
    tables and every scheduled run would fail with no explanation the admin can act on.
    """
    lowered = message.lower()
    if not any(marker in lowered for marker in _PERMISSION_MARKERS):
        return
    try:
        provisioning_probe.verify_robot_on_project(
            connection['hub_id'], connection['project_id'],
        )
    except Exception as exc:
        logger.warning('Re-verification after a permissions failure failed: %s', exc)


def run_connection_sync(
    connection_id: str,
    *,
    mode: str,
    trigger_type: str = 'manual',
    user_token: str | None = None,
) -> dict:
    """Sync one connection. Raises on failure; routes run this on a background thread."""
    if mode not in ('snapshot', 'cdc'):
        raise ValueError(f'Unknown sync mode: {mode}')

    connection = conns.get(connection_id)
    if not connection:
        raise UnknownConnection(connection_id)

    bootstrap = assert_ready(connection, conns.get_bootstrap(connection_id), mode)

    # In-flight guard first: cheap, and it must reject before any token is minted.
    in_flight = runs.has_in_flight_run(connection_id)
    if in_flight:
        raise SyncInFlight(in_flight)

    token = _resolve_token(connection_id, trigger_type, user_token)
    dbx = DatabricksClient(connection['dbx_workspace_url'], token)

    try:
        return sync_steps.run_export(
            dbx, _build_target(connection, bootstrap), build_ports(connection, dbx),
            mode=mode, trigger_type=trigger_type,
        )
    except Exception as exc:
        _reverify_on_permission_error(connection, str(exc))
        raise
