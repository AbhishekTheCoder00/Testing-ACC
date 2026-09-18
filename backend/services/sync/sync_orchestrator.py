
import json
import logging
import time
from datetime import datetime, timezone
from backend.clients import acc_client
from backend.repositories import state_store as db
from backend.clients.databricks_client import DatabricksClient, DOWNLOAD_ROLE_CDC
from backend.utils.databricks_auth import apply_valid_dbx_token_to_client, get_valid_dbx_token

logger = logging.getLogger(__name__)

#################################################

from .sync_config import (
    DC_WATERMARK_KEY,
    DC_CDC_WATERMARK_KEY,
    MANUAL_FULL_WATERMARK_KEY,
    MANUAL_FULL_MIN_INTERVAL_SEC,
    _LEGACY_VOLUME_BASE,
    _ENABLE_NOTEBOOK_DOWNLOAD,
    workflow_max_wait_min,
    snapshot_pipeline_max_wait_min,
    cdc_pipeline_max_wait_min,
    dc_snapshot_service_groups,
    dc_cdc_service_groups,
    _SYNC_TEST_SUBSET,
    TEST_SNAPSHOT_EXPORT_GROUPS,
    TEST_CDC_EXPORT_GROUPS,
    is_sync_test_subset_enabled,
)

from .download_service import (
    _phase2_in_flask,
    _phase2_via_notebook,
)

from .schema_service import (
     _load_schema_doc_from_zip,
)

from .registry_service import (
    _seed_registry_from_schema,
)

from .pipeline_config import (
    apply_and_start_pipeline_update,
    build_cdc_pipeline_conf,
    build_snapshot_pipeline_conf,
)

from .watermark_service import (
    commit_successful_watermarks_u2m,
    resolve_incremental_window,
)

from . import sync_steps

#############################################

def run_sync(user_id: str, trigger_type: str = 'manual') -> dict:
    """Manual (or auto) full Standard export → snapshot AUTO CDC pipeline."""
    return _run_dc_export(user_id, trigger_type, mode='snapshot')


def run_cdc_sync(user_id: str, trigger_type: str = 'auto') -> dict:
    """CDC-beta export → AUTO CDC pipeline (daily toggle / scheduler)."""
    return _run_dc_export(user_id, trigger_type, mode='cdc')


def _run_dc_export(user_id: str, trigger_type: str, mode: str) -> dict:
    """Dispatch to the shared step library or the pre-refactor path.

    ``USE_SHARED_STEPS`` lets the extraction be rolled back at runtime. Both branches must
    behave identically; tests/test_sync_steps.py records every Databricks call, ACC call and
    run-state transition from each and compares them. Delete this switch and
    ``_run_dc_export_legacy`` after the staging soak — see ADR.md (Phase 1).
    """
    from backend.services.bootstrap_service import use_shared_steps

    if use_shared_steps():
        return _run_dc_export_shared(user_id, trigger_type, mode)
    return _run_dc_export_legacy(user_id, trigger_type, mode)


# Columns that actually exist on the user-scoped sync_runs table. The shared steps also
# report `phase`, which only the connection-scoped table has, so it is dropped here rather
# than changing the U2M schema.
_U2M_RUN_FIELDS = frozenset({'record_counts', 'bronze_run_id', 'error', 'skipped_modules'})


def _u2m_ports(user_id: str, acc_cfg: dict, dbx: DatabricksClient) -> sync_steps.SyncPorts:
    """Bind the shared steps to the user-scoped tables and the user's OAuth tokens."""
    project_id = acc_cfg['project_id']

    def update_run(run_id, state, **kwargs):
        db.update_sync_run(
            run_id, state, **{k: v for k, v in kwargs.items() if k in _U2M_RUN_FIELDS},
        )

    return sync_steps.SyncPorts(
        get_acc_token=lambda refresh=False: acc_client.get_valid_token(
            user_id, force_refresh=refresh,
        ),
        refresh_dbx=lambda: apply_valid_dbx_token_to_client(user_id, dbx),
        create_run=lambda mode, trigger, watermark_key: db.create_sync_run(
            user_id, project_id, [watermark_key], trigger_type=trigger,
        ),
        update_run=update_run,
        resolve_window=lambda watermark_key: resolve_incremental_window(
            user_id, project_id, watermark_key,
        ),
        commit_watermarks=lambda mode, trigger: commit_successful_watermarks_u2m(
            user_id, project_id, mode=mode, trigger_type=trigger,
        ),
        assert_rate_limit=lambda mode, trigger: _assert_manual_full_rate_limit(
            user_id, project_id, trigger,
        ),
    )


def _run_dc_export_shared(user_id: str, trigger_type: str, mode: str) -> dict:
    """U2M driver over the shared sync steps — owns credential and identity resolution only."""
    if mode not in ('snapshot', 'cdc'):
        raise ValueError(f'Unknown sync mode: {mode}')

    acc_cfg = db.get_acc_config(user_id)
    bs_state = db.get_bootstrap_state(user_id)

    if not acc_cfg:
        raise RuntimeError('ACC not configured — complete ACC connection first')
    try:
        dbx_tok = get_valid_dbx_token(user_id)
    except RuntimeError as exc:
        raise RuntimeError(
            'Databricks not configured — complete Databricks connection first'
        ) from exc
    if not bs_state:
        raise RuntimeError('Bootstrap not complete — run workspace bootstrap first')

    if mode == 'snapshot':
        if not bs_state.get('snapshot_pipeline_id'):
            raise RuntimeError(
                'Bootstrap not complete or out of date — re-run bootstrap to '
                'create the snapshot pipeline.'
            )
    else:
        if not db.is_daily_cdc_enabled(user_id):
            raise RuntimeError('Daily CDC sync is disabled — enable Auto CDC first.')
        if not bs_state.get('cdc_pipeline_id'):
            raise RuntimeError(
                'CDC pipeline not provisioned — re-run bootstrap to create the '
                'CDC AUTO CDC pipeline.'
            )

    if _ENABLE_NOTEBOOK_DOWNLOAD:
        wf_key = 'snapshot_workflow_id' if mode == 'snapshot' else 'cdc_workflow_id'
        if not bs_state.get(wf_key):
            raise RuntimeError(
                'ENABLE_NOTEBOOK_DOWNLOAD is set but sync workflows were not '
                'provisioned — re-run bootstrap.'
            )

    project_id = acc_cfg['project_id']
    account_id = acc_cfg.get('acc_account_id') or ''
    if not account_id:
        hub_id = acc_cfg.get('hub_id', '')
        account_id = hub_id[2:] if hub_id.startswith('b.') else hub_id

    dbx = DatabricksClient(dbx_tok['workspace_url'], dbx_tok['access_token'])
    target = sync_steps.SyncTarget(
        account_id=account_id,
        project_id=project_id,
        bare_project_id=project_id[2:] if project_id.startswith('b.') else project_id,
        catalog_name=bs_state.get('catalog_name') or '',
        volume_base=bs_state.get('volume_path') or _LEGACY_VOLUME_BASE,
        snapshot_pipeline_id=bs_state.get('snapshot_pipeline_id'),
        cdc_pipeline_id=bs_state.get('cdc_pipeline_id'),
        snapshot_workflow_id=bs_state.get('snapshot_workflow_id'),
        cdc_workflow_id=bs_state.get('cdc_workflow_id'),
        warehouse_id=bs_state.get('warehouse_id'),
    )
    return sync_steps.run_export(
        dbx, target, _u2m_ports(user_id, acc_cfg, dbx),
        mode=mode, trigger_type=trigger_type,
    )


def _assert_snapshot_pipelines_idle(dbx: DatabricksClient, bs_state: dict) -> None:
    """Block sync while either snapshot or CDC pipeline has an active update."""
    for label, pipeline_id in (
        ('snapshot', bs_state.get('snapshot_pipeline_id')),
        ('CDC', bs_state.get('cdc_pipeline_id')),
    ):
        if not pipeline_id:
            continue
        if dbx.pipeline_has_active_update(pipeline_id):
            raise RuntimeError(
                f'Sync blocked: the {label} AUTO CDC pipeline is running '
                f'(pipeline {pipeline_id}). Wait for it to finish or cancel '
                f'the update in Databricks.'
            )


def _upload_dc_job(
    dbx: DatabricksClient,
    get_acc_token,
    account_id: str,
    job_id: str,
    vol_dir: str,
    *,
    force_flask: bool = False,
) -> tuple[str | None, int, bytes | None]:
    """Land one DC job's files in UC Volume. Returns (manifest, count, extract_zip)."""
    if _ENABLE_NOTEBOOK_DOWNLOAD and not force_flask:
        manifest_path, file_count = _phase2_via_notebook(
            dbx, get_acc_token, account_id, job_id, vol_dir,
        )
        return manifest_path, file_count, None
    extract_zip, file_count = _phase2_in_flask(
        dbx, get_acc_token, account_id, job_id, vol_dir,
    )
    return None, file_count, extract_zip


def _run_single_dc_job(
    get_acc_token,
    account_id: str,
    bare_project_id: str,
    *,
    cdc: bool,
    start_date: str | None,
    end_date: str | None = None,
    service_groups: list | None = None,
) -> tuple[str, str]:
    """Submit and poll one DC export. Returns (request_id, job_id)."""
    token = get_acc_token()
    if cdc:
        request_id = acc_client.dc_create_cdc_request(
            token, account_id, bare_project_id,
            start_date=start_date, end_date=end_date,
            service_groups=service_groups,
        )
    else:
        request_id = acc_client.dc_create_request(
            token, account_id, bare_project_id,
            start_date=start_date, end_date=end_date,
            service_groups=service_groups,
        )
    job_id = acc_client.dc_wait_for_job(get_acc_token, account_id, request_id)
    acc_client.dc_poll_job(get_acc_token, account_id, job_id)
    logger.info(
        '[sync-phase] dc_export complete type=%s job_id=%s groups=%s start=%s end=%s',
        'cdc' if cdc else 'snapshot',
        job_id,
        service_groups or ('default' if cdc else 'snapshot-default'),
        start_date or 'FULL',
        end_date or 'now',
    )
    return request_id, job_id


def _poll_pipeline_or_raise(
    dbx: DatabricksClient,
    pipeline_id: str,
    update_id: str,
    *,
    label: str,
    max_wait_min: int,
    before_poll,
) -> None:
    """Poll a pipeline update to completion or raise with an actionable message."""
    try:
        state = dbx.poll_pipeline_update(
            pipeline_id,
            update_id,
            max_wait_min=max_wait_min,
            before_poll=before_poll,
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f'{label} pipeline update {update_id} did not finish within '
            f'{max_wait_min} minutes (connector wait limit). The pipeline may '
            f'still be running in Databricks — open Lakeflow Pipelines, '
            f'pipeline {pipeline_id}, update {update_id}.'
        ) from exc
    if state != 'COMPLETED':
        raise RuntimeError(
            f'{label} pipeline update {update_id} finished with state: {state}'
        )


def _assert_manual_full_rate_limit(user_id: str, project_id: str,
                                   trigger_type: str) -> None:
    """Manual full export allowed at most once per 24 hours (optional use)."""
    if trigger_type != 'manual':
        return
    last = db.get_watermark(user_id, project_id, MANUAL_FULL_WATERMARK_KEY)
    if not last:
        return
    elapsed = time.time() - last
    if elapsed < MANUAL_FULL_MIN_INTERVAL_SEC:
        hours_left = (MANUAL_FULL_MIN_INTERVAL_SEC - elapsed) / 3600
        raise RuntimeError(
            f'Manual full sync was already run within the last 24 hours. '
            f'Try again in about {hours_left:.1f} hour(s).'
        )


# ---------------------------------------------------------------------------
# Bulk path — orchestrator
# ---------------------------------------------------------------------------


def _run_dc_export_legacy(user_id: str, trigger_type: str, mode: str) -> dict:
    """Run Data Connector export → volume → Bronze pipeline (snapshot or CDC).

    PRE-REFACTOR PATH — kept only until USE_SHARED_STEPS is retired. Any change here must
    also be made in backend/services/sync/sync_steps.py, which is the copy the M2M path uses
    and the copy that survives.
    """
    if mode not in ('snapshot', 'cdc'):
        raise ValueError(f'Unknown sync mode: {mode}')

    acc_cfg  = db.get_acc_config(user_id)
    bs_state = db.get_bootstrap_state(user_id)

    if not acc_cfg:
        raise RuntimeError('ACC not configured — complete ACC connection first')
    try:
        dbx_tok = get_valid_dbx_token(user_id)
    except RuntimeError as exc:
        raise RuntimeError(
            'Databricks not configured — complete Databricks connection first'
        ) from exc
    if not bs_state:
        raise RuntimeError('Bootstrap not complete — run workspace bootstrap first')

    if mode == 'snapshot':
        if not bs_state.get('snapshot_pipeline_id'):
            raise RuntimeError(
                'Bootstrap not complete or out of date — re-run bootstrap to '
                'create the snapshot pipeline.'
            )
        pipeline_id = bs_state['snapshot_pipeline_id']
        volume_segment = 'data_connector'
        path_conf_key = 'acc.dc_snapshot_path'
        dc_watermark_key = DC_WATERMARK_KEY
    else:
        if not db.is_daily_cdc_enabled(user_id):
            raise RuntimeError('Daily CDC sync is disabled — enable Auto CDC first.')
        if not bs_state.get('cdc_pipeline_id'):
            raise RuntimeError(
                'CDC pipeline not provisioned — re-run bootstrap to create the '
                'CDC AUTO CDC pipeline.'
            )
        pipeline_id = bs_state['cdc_pipeline_id']
        volume_segment = 'data_connector_cdc'
        path_conf_key = 'acc.dc_cdc_path'
        dc_watermark_key = DC_CDC_WATERMARK_KEY

    if _ENABLE_NOTEBOOK_DOWNLOAD:
        wf_key = 'snapshot_workflow_id' if mode == 'snapshot' else 'cdc_workflow_id'
        if not bs_state.get(wf_key):
            raise RuntimeError(
                'ENABLE_NOTEBOOK_DOWNLOAD is set but sync workflows were not '
                'provisioned — re-run bootstrap.'
            )

    volume_base = bs_state.get('volume_path') or _LEGACY_VOLUME_BASE
    project_id    = acc_cfg['project_id']
    account_id    = acc_cfg.get('acc_account_id') or ''
    workspace_url = dbx_tok['workspace_url']
    pat           = dbx_tok['access_token']

    if not account_id:
        hub_id     = acc_cfg.get('hub_id', '')
        account_id = hub_id[2:] if hub_id.startswith('b.') else hub_id

    bare_project_id = project_id[2:] if project_id.startswith('b.') else project_id
    dbx   = DatabricksClient(workspace_url, pat)
    _refresh_dbx = lambda: apply_valid_dbx_token_to_client(user_id, dbx)
    get_acc_token = lambda refresh=False: acc_client.get_valid_token(
        user_id, force_refresh=refresh,
    )

    _assert_snapshot_pipelines_idle(dbx, bs_state)
    if mode == 'snapshot':
        _assert_manual_full_rate_limit(user_id, project_id, trigger_type)

    run_id = db.create_sync_run(user_id, project_id, [dc_watermark_key],
                                trigger_type=trigger_type)
    db.update_sync_run(run_id, 'running')
    logger.info(
        '[sync-phase] run_id=%s phase=started mode=%s trigger=%s user=%s project=%s',
        run_id, mode, trigger_type, user_id, bare_project_id,
    )
    if _SYNC_TEST_SUBSET:
        logger.warning(
            '[sync-test-subset] SYNC_TEST_SUBSET=true — snapshot=%s cdc=%s '
            '(set SYNC_TEST_SUBSET=false to restore full export)',
            TEST_SNAPSHOT_EXPORT_GROUPS,
            TEST_CDC_EXPORT_GROUPS,
        )

    sync_phase = 'started'
    try:
        sync_phase = 'resolve_window'
        start_date, end_date = resolve_incremental_window(
            user_id, project_id, dc_watermark_key,
        )
        if mode == 'cdc':
            logger.info(
                '[sync-cdc] run_id=%s full_refresh=false incremental_start=%s vol_segment=%s',
                run_id,
                start_date or 'FULL_EXPORT',
                volume_segment,
            )

        sync_phase = 'dc_export'
        db.update_sync_run(run_id, 'dc_job_submitted')
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')
        vol_dir_cdc = None
        manifest_path = None
        cdc_manifest_path = None
        extract_zip = None
        file_count = 0

        if mode == 'snapshot':
            # Job 1: snapshot-only groups (Pipeline A).
            # Job 2: cdc* groups for Pipeline B ONCE baseline + ongoing.
            snap_start = start_date
            snap_end = end_date
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export snapshot start=%s end=%s',
                run_id, snap_start or 'FULL', snap_end or 'now',
            )
            _, snap_job_id = _run_single_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=False,
                start_date=snap_start, end_date=snap_end,
                service_groups=dc_snapshot_service_groups(),
            )
            sync_phase = 'upload_snapshot'
            db.update_sync_run(run_id, 'uploading_files')
            vol_dir = f'{volume_base}/data_connector/{bare_project_id}/{timestamp}'
            manifest_path, snap_count, _ = _upload_dc_job(
                dbx, get_acc_token, account_id, snap_job_id, vol_dir,
            )
            file_count += snap_count

            sync_phase = 'resolve_cdc_window'
            cdc_start, cdc_end = resolve_incremental_window(
                user_id, project_id, DC_CDC_WATERMARK_KEY,
            )
            sync_phase = 'dc_export_cdc_baseline'
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export cdc_baseline start=%s end=%s',
                run_id, cdc_start or 'FULL', cdc_end or 'now',
            )
            _, cdc_job_id = _run_single_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=True,
                start_date=cdc_start, end_date=cdc_end,
                service_groups=dc_cdc_service_groups(),
            )
            vol_dir_cdc = f'{volume_base}/data_connector_cdc/{bare_project_id}/{timestamp}'
            cdc_manifest_path, cdc_count, _ = _upload_dc_job(
                dbx, get_acc_token, account_id, cdc_job_id, vol_dir_cdc,
            )
            file_count += cdc_count
        else:
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export cdc start=%s end=%s',
                run_id, start_date or 'FULL', end_date or 'now',
            )
            _, job_id = _run_single_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=True,
                start_date=start_date, end_date=end_date,
                service_groups=dc_cdc_service_groups(),
            )
            sync_phase = 'upload_cdc'
            db.update_sync_run(run_id, 'uploading_files')
            vol_dir = f'{volume_base}/{volume_segment}/{bare_project_id}/{timestamp}'
            manifest_path, file_count, extract_zip = _upload_dc_job(
                dbx, get_acc_token, account_id, job_id, vol_dir,
            )

        logger.info(
            '[sync-phase] run_id=%s phase=upload_complete files=%d snapshot_vol=%s cdc_vol=%s',
            run_id, file_count, vol_dir, vol_dir_cdc or 'n/a',
        )
        if _ENABLE_NOTEBOOK_DOWNLOAD and mode == 'snapshot':
            logger.info(
                'Notebook path: manifests snapshot=%s cdc=%s — '
                'workflow tasks: bulk_download → bulk_download_cdc → seed_registry → '
                'snapshot_pipeline → cdc_pipeline (bronze tables created by pipelines, not download)',
                manifest_path,
                cdc_manifest_path,
            )

        if not _ENABLE_NOTEBOOK_DOWNLOAD:
            warehouse_id = bs_state.get('warehouse_id')
            catalog_name = bs_state.get('catalog_name')
            if extract_zip and warehouse_id and catalog_name:
                try:
                    logger.info("Loading schema from ZIP for PK registry...")
                    schema_doc = _load_schema_doc_from_zip(extract_zip)
                    if schema_doc is not None:
                        n_rows, n_batches = _seed_registry_from_schema(
                            dbx, warehouse_id, catalog_name, schema_doc,
                        )
                        logger.info(
                            'PK registry MERGE complete: %d entries, %d batch(es)',
                            n_rows, n_batches,
                        )
                except Exception as exc:
                    logger.exception(
                        'PK registry update failed (continuing with sync): %s', exc,
                    )

        db.update_sync_run(run_id, 'bronze_written', record_counts=json.dumps({'files': file_count}))

        if _ENABLE_NOTEBOOK_DOWNLOAD:
            sync_phase = 'workflow'
            workflow_id = (
                bs_state['snapshot_workflow_id']
                if mode == 'snapshot' else bs_state['cdc_workflow_id']
            )
            catalog_name = bs_state.get('catalog_name') or ''
            base_conf = build_snapshot_pipeline_conf(catalog_name, bare_project_id)
            wf_wait_min = workflow_max_wait_min()
            logger.info(
                '[sync-phase] run_id=%s phase=workflow mode=%s job_id=%s wait_min=%d',
                run_id, mode, workflow_id, wf_wait_min,
            )
            db.update_sync_run(run_id, 'workflow_triggered')
            if mode == 'snapshot':
                if not bs_state.get('cdc_pipeline_id'):
                    raise RuntimeError(
                        'CDC pipeline not provisioned — re-run bootstrap for Pipeline B.'
                    )
                snapshot_pipeline_id = bs_state['snapshot_pipeline_id']
                cdc_pipeline_id = bs_state['cdc_pipeline_id']
                pipeline_configs = {
                    snapshot_pipeline_id: {
                        **base_conf,
                        'acc.dc_snapshot_path': vol_dir,
                    },
                    cdc_pipeline_id: {
                        **build_cdc_pipeline_conf(catalog_name, bare_project_id),
                        'acc.dc_snapshot_path': vol_dir,
                        'acc.dc_cdc_path':      vol_dir_cdc,
                    },
                }
                if not manifest_path or not cdc_manifest_path:
                    raise RuntimeError(
                        'Snapshot sync requires both snapshot and CDC baseline manifests '
                        'when ENABLE_NOTEBOOK_DOWNLOAD is set — re-run sync or check DC export.'
                    )
                logger.info(
                    'Pipeline paths for this run: acc.dc_snapshot_path=%s acc.dc_cdc_path=%s',
                    vol_dir,
                    vol_dir_cdc,
                )
                wf_run_id = dbx.run_combined_workflow(
                    workflow_id,
                    pipeline_configs=pipeline_configs,
                    manifest_path=manifest_path,
                    cdc_manifest_path=cdc_manifest_path,
                )
            else:
                if not manifest_path:
                    raise RuntimeError(
                        'CDC sync requires a manifest when ENABLE_NOTEBOOK_DOWNLOAD is set — '
                        're-run sync or check DC export.'
                    )
                pipeline_conf = {
                    **build_cdc_pipeline_conf(
                        bs_state.get('catalog_name') or '', bare_project_id,
                    ),
                    path_conf_key: vol_dir,
                }
                logger.info(
                    'Pipeline path for this run: %s=%s manifest=%s',
                    path_conf_key,
                    vol_dir,
                    manifest_path,
                )
                wf_run_id = dbx.run_workflow(
                    workflow_id,
                    pipeline_id=pipeline_id,
                    manifest_path=manifest_path,
                    pipeline_configuration=pipeline_conf,
                    download_role=DOWNLOAD_ROLE_CDC,
                )
            db.update_sync_run(run_id, 'workflow_running', bronze_run_id=wf_run_id)
            wf_result = dbx.poll_workflow_run(
                wf_run_id,
                max_wait_min=wf_wait_min,
                before_poll=_refresh_dbx,
            )
            if wf_result != 'SUCCESS':
                raise RuntimeError(
                    f'Sync workflow run {wf_run_id} finished with result_state: {wf_result}. '
                    f'CSVs in the volume do not create bronze tables — check Databricks job '
                    f'task logs for bulk_download / snapshot_pipeline / cdc_pipeline. '
                    f'Both bulk_download tasks must finish (_SUCCESS in each vol folder) '
                    f'before pipelines run.'
                )
        else:
            sync_phase = 'pipeline'
            logger.info(
                '[sync-phase] run_id=%s phase=pipeline mode=%s notebook_download=false',
                run_id, mode,
            )
            db.update_sync_run(run_id, 'bronze_job_triggered')
            catalog_name = bs_state.get('catalog_name') or ''
            if mode == 'snapshot':
                snap_conf = {
                    **build_snapshot_pipeline_conf(catalog_name, bare_project_id),
                    'acc.dc_snapshot_path': vol_dir,
                }
                snap_update = apply_and_start_pipeline_update(
                    dbx,
                    bs_state['snapshot_pipeline_id'],
                    snap_conf,
                    label='snapshot pipeline',
                )
                db.update_sync_run(run_id, 'bronze_job_running', bronze_run_id=snap_update)
                _poll_pipeline_or_raise(
                    dbx,
                    bs_state['snapshot_pipeline_id'],
                    snap_update,
                    label='Snapshot',
                    max_wait_min=snapshot_pipeline_max_wait_min(),
                    before_poll=_refresh_dbx,
                )
                cdc_conf = {
                    **build_cdc_pipeline_conf(catalog_name, bare_project_id),
                    'acc.dc_snapshot_path': vol_dir,
                    'acc.dc_cdc_path':      vol_dir_cdc,
                }
                sync_phase = 'pipeline_b_baseline'
                cdc_update = apply_and_start_pipeline_update(
                    dbx,
                    bs_state['cdc_pipeline_id'],
                    cdc_conf,
                    label='CDC pipeline',
                    full_refresh=True,
                )
                db.update_sync_run(run_id, 'bronze_job_running', bronze_run_id=cdc_update)
                _poll_pipeline_or_raise(
                    dbx,
                    bs_state['cdc_pipeline_id'],
                    cdc_update,
                    label='CDC',
                    max_wait_min=cdc_pipeline_max_wait_min(),
                    before_poll=_refresh_dbx,
                )
            else:
                sync_phase = 'pipeline_b_incremental'
                pipeline_conf = build_cdc_pipeline_conf(
                    catalog_name,
                    bare_project_id,
                    **{path_conf_key: vol_dir},
                )
                cdc_full_refresh = is_sync_test_subset_enabled()
                logger.info(
                    '[sync-cdc] run_id=%s pipeline_id=%s full_refresh=%s path=%s',
                    run_id, pipeline_id, cdc_full_refresh, vol_dir,
                )
                update_id = apply_and_start_pipeline_update(
                    dbx,
                    pipeline_id,
                    pipeline_conf,
                    label='CDC pipeline',
                    full_refresh=cdc_full_refresh,
                )
                db.update_sync_run(run_id, 'bronze_job_running', bronze_run_id=update_id)
                _poll_pipeline_or_raise(
                    dbx,
                    pipeline_id,
                    update_id,
                    label='CDC',
                    max_wait_min=cdc_pipeline_max_wait_min(),
                    before_poll=_refresh_dbx,
                )

        db.update_sync_run(
            run_id,
            'complete',
            record_counts=json.dumps({'files': file_count, 'mode': mode}),
        )
        commit_successful_watermarks_u2m(
            user_id,
            project_id,
            mode=mode,
            trigger_type=trigger_type,
        )
        logger.info(
            '[sync-phase] run_id=%s phase=complete mode=%s files=%d',
            run_id, mode, file_count,
        )

        return {
            'mode':       mode,
            'run_id':     run_id,
            'state':      'complete',
            'file_count': file_count,
        }

    except Exception as exc:
        error_msg = str(exc)
        logger.exception(
            '[sync-failed] run_id=%s mode=%s trigger=%s project=%s phase=%s error=%s',
            run_id,
            mode,
            trigger_type,
            bare_project_id,
            sync_phase,
            error_msg,
        )
        db.update_sync_run(run_id, 'failed', error=error_msg)
        raise


