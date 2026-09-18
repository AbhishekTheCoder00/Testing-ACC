"""Reconcile orphaned U2M sync runs against Databricks when the background thread died.

The sync orchestrator updates SQLite only after its polling loop finishes. If the
Flask/Gunicorn worker restarts mid-run, the Databricks job may succeed while the
local row stays at ``workflow_running`` (or ``bronze_job_running``). Status polls
call into this module to heal those rows.
"""
from __future__ import annotations

import json
import logging
import time

from backend.clients.databricks_client import DatabricksClient
from backend.repositories import state_store as db
from backend.repositories.state.sync_repository import _sync_run_mode
from backend.utils.databricks_auth import create_databricks_client_for_user
from backend.services.sync.watermark_service import commit_successful_watermarks_u2m

_WORKFLOW_TERMINAL_LC = DatabricksClient._WORKFLOW_TERMINAL_LC

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({'complete', 'failed', 'partial'})
_WORKFLOW_STATES = frozenset({'workflow_running', 'workflow_triggered'})
_PIPELINE_STATES = frozenset({'bronze_job_running'})
_PIPELINE_TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELED'})
_RECONCILE_MIN_INTERVAL_SEC = 60

_last_reconcile_at: dict[int, float] = {}


def _should_attempt_reconcile(run_id: int) -> bool:
    now = time.time()
    last = _last_reconcile_at.get(run_id, 0.0)
    if now - last < _RECONCILE_MIN_INTERVAL_SEC:
        return False
    _last_reconcile_at[run_id] = now
    return True


def _reload_run(run_id: int) -> dict | None:
    return db.get_sync_run(run_id)


def _parse_record_counts(run: dict) -> dict:
    raw = run.get('record_counts')
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


def _complete_u2m_run(
    run: dict,
    *,
    user_id: str,
    project_id: str,
    mode: str,
) -> dict | None:
    run_id = run['run_id']
    rc = _parse_record_counts(run)
    rc.setdefault('mode', mode)
    rc['reconciled'] = True
    db.update_sync_run(
        run_id,
        'complete',
        record_counts=json.dumps(rc),
    )
    commit_successful_watermarks_u2m(
        user_id,
        project_id,
        mode=mode,
        trigger_type=run.get('trigger_type') or 'manual',
    )
    logger.info(
        '[sync-reconcile] run_id=%s marked complete (Databricks already succeeded)',
        run_id,
    )
    return _reload_run(run_id)


def _fail_u2m_run(run: dict, error: str) -> dict | None:
    run_id = run['run_id']
    db.update_sync_run(run_id, 'failed', error=error)
    logger.info('[sync-reconcile] run_id=%s marked failed: %s', run_id, error)
    return _reload_run(run_id)


def _get_pipeline_update_state(dbx, pipeline_id: str, update_id: str) -> str | None:
    try:
        payload = dbx.get_pipeline_update(pipeline_id, update_id)
    except Exception as exc:
        logger.debug(
            'Pipeline update lookup failed pipeline=%s update=%s: %s',
            pipeline_id,
            update_id,
            exc,
        )
        return None
    update = payload.get('update', payload)
    return (update.get('state') or '').upper() or None


def _reconcile_workflow_run(dbx, run: dict, user_id: str, project_id: str) -> dict | None:
    wf_run_id = run.get('bronze_run_id')
    if not wf_run_id:
        return None
    try:
        lc_state, res_state = dbx.get_workflow_run_state(int(wf_run_id))
    except Exception as exc:
        logger.warning(
            '[sync-reconcile] workflow run %s lookup failed run_id=%s: %s',
            wf_run_id,
            run['run_id'],
            exc,
        )
        return None

    if lc_state not in _WORKFLOW_TERMINAL_LC:
        return None

    mode = _sync_run_mode(run)
    if res_state == 'SUCCESS':
        return _complete_u2m_run(
            run, user_id=user_id, project_id=project_id, mode=mode,
        )

    msg = (
        f'Sync workflow run {wf_run_id} finished with result_state: {res_state or lc_state}. '
        f'Reconciled after connector process exited mid-run.'
    )
    return _fail_u2m_run(run, msg)


def _reconcile_pipeline_run(
    dbx,
    run: dict,
    user_id: str,
    project_id: str,
    bs_state: dict,
) -> dict | None:
    update_id = run.get('bronze_run_id')
    if not update_id:
        return None
    update_id = str(update_id)
    mode = _sync_run_mode(run)
    snap_pid = bs_state.get('snapshot_pipeline_id')
    cdc_pid = bs_state.get('cdc_pipeline_id')

    snap_state = (
        _get_pipeline_update_state(dbx, snap_pid, update_id) if snap_pid else None
    )
    cdc_state = (
        _get_pipeline_update_state(dbx, cdc_pid, update_id) if cdc_pid else None
    )

    state = cdc_state or snap_state
    on_cdc = cdc_state is not None
    on_snap = snap_state is not None and cdc_state is None

    if state is None:
        return None
    if state not in _PIPELINE_TERMINAL:
        return None
    if state != 'COMPLETED':
        return _fail_u2m_run(
            run,
            f'Pipeline update {update_id} finished with state: {state} '
            f'(reconciled after connector process exited mid-run).',
        )

    if mode == 'cdc' and on_cdc:
        return _complete_u2m_run(
            run, user_id=user_id, project_id=project_id, mode=mode,
        )
    if mode == 'snapshot' and on_cdc:
        return _complete_u2m_run(
            run, user_id=user_id, project_id=project_id, mode=mode,
        )
    if mode == 'snapshot' and on_snap:
        # Snapshot pipeline update finished; CDC baseline must also be COMPLETED.
        if not cdc_pid:
            return None
        if not _pipeline_has_completed_update_since(dbx, cdc_pid, run.get('started_at', 0)):
            return None
        return _complete_u2m_run(
            run, user_id=user_id, project_id=project_id, mode=mode,
        )
    return None


def _pipeline_has_completed_update_since(dbx, pipeline_id: str, since_unix: float) -> bool:
    """Return True if the pipeline has a COMPLETED update at or after ``since_unix``."""
    since_ms = int(float(since_unix) * 1000)
    page_token = None
    while True:
        params: dict = {'max_results': 25}
        if page_token:
            params['page_token'] = page_token
        try:
            data = dbx.get(f'/api/2.0/pipelines/{pipeline_id}/updates', params=params)
        except Exception as exc:
            logger.warning(
                'Pipeline updates list failed pipeline=%s: %s', pipeline_id, exc,
            )
            return False
        for entry in (data.get('updates') or []):
            state = (entry.get('state') or '').upper()
            creation = entry.get('creation_time') or 0
            if state == 'COMPLETED' and creation >= since_ms:
                return True
        page_token = data.get('next_page_token')
        if not page_token:
            return False


def reconcile_u2m_sync_run(user_id: str, run: dict | None) -> dict | None:
    """Advance a stuck sync run when Databricks already reached a terminal state."""
    if not run:
        return run
    state = run.get('state') or ''
    if state in _TERMINAL:
        return run
    if state not in _WORKFLOW_STATES and state not in _PIPELINE_STATES:
        return run
    if not _should_attempt_reconcile(int(run['run_id'])):
        return run

    project_id = run.get('project_id')
    if not project_id:
        return run

    bs_state = db.get_bootstrap_state(user_id)
    if not bs_state:
        return run

    try:
        dbx = create_databricks_client_for_user(user_id)
    except Exception as exc:
        logger.warning(
            '[sync-reconcile] cannot build Databricks client user=%s: %s',
            user_id,
            exc,
        )
        return run

    if state in _WORKFLOW_STATES:
        updated = _reconcile_workflow_run(dbx, run, user_id, project_id)
        return updated or run

    updated = _reconcile_pipeline_run(dbx, run, user_id, project_id, bs_state)
    return updated or run
