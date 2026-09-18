"""
Purpose: The Data Connector export → UC Volume → Bronze pipeline sequence, once, for both auth
paths. Extracted from sync_orchestrator._run_dc_export with behaviour, call order and run-state
names unchanged.

Sync differs between the paths in more places than bootstrap does — credentials *and* identity
*and* which tables hold the run row and watermarks — so the seam is a SyncTarget (what to sync)
plus SyncPorts (the handful of identity-bound callables). Everything else, including both
download modes, lives here. The U2M driver passes ports backed by user-keyed tables; the M2M
driver passes ports backed by connection-keyed tables and translates the run-state vocabulary.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from backend.clients import acc_client
from backend.clients.databricks_client import DOWNLOAD_ROLE_CDC, DatabricksClient

from .download_service import _phase2_in_flask, _phase2_via_notebook
from .pipeline_config import (
    apply_and_start_pipeline_update,
    build_cdc_pipeline_conf,
    build_snapshot_pipeline_conf,
)
from .registry_service import _seed_registry_from_schema
from .schema_service import _load_schema_doc_from_zip
from .sync_config import (
    DC_CDC_WATERMARK_KEY,
    DC_WATERMARK_KEY,
    _ENABLE_NOTEBOOK_DOWNLOAD,
    _SYNC_TEST_SUBSET,
    TEST_CDC_EXPORT_GROUPS,
    TEST_SNAPSHOT_EXPORT_GROUPS,
    cdc_pipeline_max_wait_min,
    dc_cdc_service_groups,
    dc_snapshot_service_groups,
    is_sync_test_subset_enabled,
    snapshot_pipeline_max_wait_min,
    workflow_max_wait_min,
)

logger = logging.getLogger(__name__)


@dataclass
class SyncTarget:
    """What to sync and where to put it — no identity, no credentials."""

    account_id: str
    project_id: str
    bare_project_id: str
    catalog_name: str
    volume_base: str
    snapshot_pipeline_id: str | None = None
    cdc_pipeline_id: str | None = None
    snapshot_workflow_id: int | None = None
    cdc_workflow_id: int | None = None
    warehouse_id: str | None = None


@dataclass
class SyncPorts:
    """The identity-bound behaviour each driver supplies.

    ``update_run`` takes the U2M state vocabulary; the M2M adapter maps it onto the FR-05
    §10.3 states its table uses. Keeping one vocabulary in the shared code means the phase
    sequence reads the same for both paths.
    """

    get_acc_token: Callable
    refresh_dbx: Callable
    create_run: Callable
    update_run: Callable
    resolve_window: Callable
    commit_watermarks: Callable
    assert_rate_limit: Callable = lambda mode, trigger_type: None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def assert_pipelines_idle(dbx: DatabricksClient, target: SyncTarget) -> None:
    """Block a new run while either pipeline still has an update in flight."""
    for label, pipeline_id in (
        ('snapshot', target.snapshot_pipeline_id),
        ('CDC', target.cdc_pipeline_id),
    ):
        if not pipeline_id:
            continue
        if dbx.pipeline_has_active_update(pipeline_id):
            raise RuntimeError(
                f'Sync blocked: the {label} AUTO CDC pipeline is running '
                f'(pipeline {pipeline_id}). Wait for it to finish or cancel '
                f'the update in Databricks.'
            )


def run_dc_job(
    get_acc_token,
    account_id: str,
    bare_project_id: str,
    *,
    cdc: bool,
    start_date: str | None,
    end_date: str | None = None,
    service_groups: list | None = None,
) -> tuple[str, str]:
    """Submit one Data Connector export and poll it to completion."""
    token = get_acc_token()
    if cdc:
        request_id = acc_client.dc_create_cdc_request(
            token, account_id, bare_project_id,
            start_date=start_date, end_date=end_date, service_groups=service_groups,
        )
    else:
        request_id = acc_client.dc_create_request(
            token, account_id, bare_project_id,
            start_date=start_date, end_date=end_date, service_groups=service_groups,
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


def land_files(
    dbx: DatabricksClient,
    get_acc_token,
    account_id: str,
    job_id: str,
    vol_dir: str,
    *,
    force_flask: bool = False,
) -> tuple[str | None, int, bytes | None]:
    """Get one DC job's files into the UC Volume.

    Two modes, both retained (the notebook path keeps ACC bytes out of the control plane;
    the Flask path is the current default). Returns (manifest, count, extract_zip).
    """
    if _ENABLE_NOTEBOOK_DOWNLOAD and not force_flask:
        manifest_path, file_count = _phase2_via_notebook(
            dbx, get_acc_token, account_id, job_id, vol_dir,
        )
        return manifest_path, file_count, None
    extract_zip, file_count = _phase2_in_flask(
        dbx, get_acc_token, account_id, job_id, vol_dir,
    )
    return None, file_count, extract_zip


def poll_pipeline_or_raise(
    dbx: DatabricksClient,
    pipeline_id: str,
    update_id: str,
    *,
    label: str,
    max_wait_min: int,
    before_poll,
) -> None:
    try:
        state = dbx.poll_pipeline_update(
            pipeline_id, update_id, max_wait_min=max_wait_min, before_poll=before_poll,
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


def seed_registry_from_extract(dbx: DatabricksClient, target: SyncTarget,
                               extract_zip: bytes | None) -> None:
    """Control-plane PK registry seed, used only on the Flask download path — on the
    notebook path seed_registry.py does this as a job task instead."""
    if not (extract_zip and target.warehouse_id and target.catalog_name):
        return
    try:
        logger.info('Loading schema from ZIP for PK registry...')
        schema_doc = _load_schema_doc_from_zip(extract_zip)
        if schema_doc is not None:
            n_rows, n_batches = _seed_registry_from_schema(
                dbx, target.warehouse_id, target.catalog_name, schema_doc,
            )
            logger.info('PK registry MERGE complete: %d entries, %d batch(es)',
                        n_rows, n_batches)
    except Exception as exc:
        logger.exception('PK registry update failed (continuing with sync): %s', exc)


# ---------------------------------------------------------------------------
# The whole export
# ---------------------------------------------------------------------------


def run_export(
    dbx: DatabricksClient,
    target: SyncTarget,
    ports: SyncPorts,
    *,
    mode: str,
    trigger_type: str,
) -> dict:
    """Data Connector export → volume → Bronze pipeline, for snapshot or CDC."""
    if mode not in ('snapshot', 'cdc'):
        raise ValueError(f'Unknown sync mode: {mode}')

    if mode == 'snapshot':
        pipeline_id = target.snapshot_pipeline_id
        volume_segment = 'data_connector'
        path_conf_key = 'acc.dc_snapshot_path'
        dc_watermark_key = DC_WATERMARK_KEY
    else:
        pipeline_id = target.cdc_pipeline_id
        volume_segment = 'data_connector_cdc'
        path_conf_key = 'acc.dc_cdc_path'
        dc_watermark_key = DC_CDC_WATERMARK_KEY

    get_acc_token = ports.get_acc_token
    volume_base = target.volume_base
    bare_project_id = target.bare_project_id
    account_id = target.account_id
    catalog_name = target.catalog_name or ''

    assert_pipelines_idle(dbx, target)
    if mode == 'snapshot':
        ports.assert_rate_limit(mode, trigger_type)

    run_id = ports.create_run(mode, trigger_type, dc_watermark_key)
    ports.update_run(run_id, 'running')
    logger.info(
        '[sync-phase] run_id=%s phase=started mode=%s trigger=%s project=%s',
        run_id, mode, trigger_type, bare_project_id,
    )
    if _SYNC_TEST_SUBSET:
        logger.warning(
            '[sync-test-subset] SYNC_TEST_SUBSET=true — snapshot=%s cdc=%s '
            '(set SYNC_TEST_SUBSET=false to restore full export)',
            TEST_SNAPSHOT_EXPORT_GROUPS, TEST_CDC_EXPORT_GROUPS,
        )

    sync_phase = 'started'
    try:
        sync_phase = 'resolve_window'
        start_date, end_date = ports.resolve_window(dc_watermark_key)
        if mode == 'cdc':
            logger.info(
                '[sync-cdc] run_id=%s full_refresh=false incremental_start=%s vol_segment=%s',
                run_id, start_date or 'FULL_EXPORT', volume_segment,
            )

        sync_phase = 'dc_export'
        ports.update_run(run_id, 'dc_job_submitted')
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%S')
        vol_dir_cdc = None
        manifest_path = None
        cdc_manifest_path = None
        extract_zip = None
        file_count = 0

        if mode == 'snapshot':
            # Two exports: the snapshot-only groups for Pipeline A, then a cdc* baseline
            # so Pipeline B has something to build its ONCE baseline from.
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export snapshot start=%s end=%s',
                run_id, start_date or 'FULL', end_date or 'now',
            )
            _, snap_job_id = run_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=False,
                start_date=start_date, end_date=end_date,
                service_groups=dc_snapshot_service_groups(),
            )
            sync_phase = 'upload_snapshot'
            ports.update_run(run_id, 'uploading_files')
            vol_dir = f'{volume_base}/data_connector/{bare_project_id}/{timestamp}'
            manifest_path, snap_count, _ = land_files(
                dbx, get_acc_token, account_id, snap_job_id, vol_dir,
            )
            file_count += snap_count

            sync_phase = 'resolve_cdc_window'
            cdc_start, cdc_end = ports.resolve_window(DC_CDC_WATERMARK_KEY)
            sync_phase = 'dc_export_cdc_baseline'
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export cdc_baseline start=%s end=%s',
                run_id, cdc_start or 'FULL', cdc_end or 'now',
            )
            _, cdc_job_id = run_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=True,
                start_date=cdc_start, end_date=cdc_end,
                service_groups=dc_cdc_service_groups(),
            )
            vol_dir_cdc = f'{volume_base}/data_connector_cdc/{bare_project_id}/{timestamp}'
            cdc_manifest_path, cdc_count, _ = land_files(
                dbx, get_acc_token, account_id, cdc_job_id, vol_dir_cdc,
            )
            file_count += cdc_count
        else:
            logger.info(
                '[sync-phase] run_id=%s phase=dc_export cdc start=%s end=%s',
                run_id, start_date or 'FULL', end_date or 'now',
            )
            _, job_id = run_dc_job(
                get_acc_token, account_id, bare_project_id, cdc=True,
                start_date=start_date, end_date=end_date,
                service_groups=dc_cdc_service_groups(),
            )
            sync_phase = 'upload_cdc'
            ports.update_run(run_id, 'uploading_files')
            vol_dir = f'{volume_base}/{volume_segment}/{bare_project_id}/{timestamp}'
            manifest_path, file_count, extract_zip = land_files(
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
                'snapshot_pipeline → cdc_pipeline (bronze tables created by pipelines, '
                'not download)',
                manifest_path, cdc_manifest_path,
            )

        if not _ENABLE_NOTEBOOK_DOWNLOAD:
            seed_registry_from_extract(dbx, target, extract_zip)

        ports.update_run(run_id, 'bronze_written',
                         record_counts=json.dumps({'files': file_count}))

        if _ENABLE_NOTEBOOK_DOWNLOAD:
            sync_phase = 'workflow'
            workflow_id = (
                target.snapshot_workflow_id if mode == 'snapshot'
                else target.cdc_workflow_id
            )
            base_conf = build_snapshot_pipeline_conf(catalog_name, bare_project_id)
            wf_wait_min = workflow_max_wait_min()
            logger.info(
                '[sync-phase] run_id=%s phase=workflow mode=%s job_id=%s wait_min=%d',
                run_id, mode, workflow_id, wf_wait_min,
            )
            ports.update_run(run_id, 'workflow_triggered')
            if mode == 'snapshot':
                if not target.cdc_pipeline_id:
                    raise RuntimeError(
                        'CDC pipeline not provisioned — re-run bootstrap for Pipeline B.'
                    )
                pipeline_configs = {
                    target.snapshot_pipeline_id: {
                        **base_conf,
                        'acc.dc_snapshot_path': vol_dir,
                    },
                    target.cdc_pipeline_id: {
                        **build_cdc_pipeline_conf(catalog_name, bare_project_id),
                        'acc.dc_snapshot_path': vol_dir,
                        'acc.dc_cdc_path': vol_dir_cdc,
                    },
                }
                if not manifest_path or not cdc_manifest_path:
                    raise RuntimeError(
                        'Snapshot sync requires both snapshot and CDC baseline manifests '
                        'when ENABLE_NOTEBOOK_DOWNLOAD is set — re-run sync or check DC export.'
                    )
                logger.info(
                    'Pipeline paths for this run: acc.dc_snapshot_path=%s acc.dc_cdc_path=%s',
                    vol_dir, vol_dir_cdc,
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
                    **build_cdc_pipeline_conf(catalog_name, bare_project_id),
                    path_conf_key: vol_dir,
                }
                logger.info('Pipeline path for this run: %s=%s manifest=%s',
                            path_conf_key, vol_dir, manifest_path)
                wf_run_id = dbx.run_workflow(
                    workflow_id,
                    pipeline_id=pipeline_id,
                    manifest_path=manifest_path,
                    pipeline_configuration=pipeline_conf,
                    download_role=DOWNLOAD_ROLE_CDC,
                )
            ports.update_run(run_id, 'workflow_running', bronze_run_id=wf_run_id)
            wf_result = dbx.poll_workflow_run(
                wf_run_id, max_wait_min=wf_wait_min, before_poll=ports.refresh_dbx,
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
            ports.update_run(run_id, 'bronze_job_triggered')
            if mode == 'snapshot':
                snap_conf = {
                    **build_snapshot_pipeline_conf(catalog_name, bare_project_id),
                    'acc.dc_snapshot_path': vol_dir,
                }
                snap_update = apply_and_start_pipeline_update(
                    dbx, target.snapshot_pipeline_id, snap_conf, label='snapshot pipeline',
                )
                ports.update_run(run_id, 'bronze_job_running', bronze_run_id=snap_update)
                poll_pipeline_or_raise(
                    dbx, target.snapshot_pipeline_id, snap_update, label='Snapshot',
                    max_wait_min=snapshot_pipeline_max_wait_min(),
                    before_poll=ports.refresh_dbx,
                )
                cdc_conf = {
                    **build_cdc_pipeline_conf(catalog_name, bare_project_id),
                    'acc.dc_snapshot_path': vol_dir,
                    'acc.dc_cdc_path': vol_dir_cdc,
                }
                sync_phase = 'pipeline_b_baseline'
                cdc_update = apply_and_start_pipeline_update(
                    dbx, target.cdc_pipeline_id, cdc_conf, label='CDC pipeline',
                    full_refresh=True,
                )
                ports.update_run(run_id, 'bronze_job_running', bronze_run_id=cdc_update)
                poll_pipeline_or_raise(
                    dbx, target.cdc_pipeline_id, cdc_update, label='CDC',
                    max_wait_min=cdc_pipeline_max_wait_min(),
                    before_poll=ports.refresh_dbx,
                )
            else:
                sync_phase = 'pipeline_b_incremental'
                pipeline_conf = build_cdc_pipeline_conf(
                    catalog_name, bare_project_id, **{path_conf_key: vol_dir},
                )
                cdc_full_refresh = is_sync_test_subset_enabled()
                logger.info(
                    '[sync-cdc] run_id=%s pipeline_id=%s full_refresh=%s path=%s',
                    run_id, pipeline_id, cdc_full_refresh, vol_dir,
                )
                update_id = apply_and_start_pipeline_update(
                    dbx, pipeline_id, pipeline_conf, label='CDC pipeline',
                    full_refresh=cdc_full_refresh,
                )
                ports.update_run(run_id, 'bronze_job_running', bronze_run_id=update_id)
                poll_pipeline_or_raise(
                    dbx, pipeline_id, update_id, label='CDC',
                    max_wait_min=cdc_pipeline_max_wait_min(),
                    before_poll=ports.refresh_dbx,
                )

        ports.update_run(
            run_id, 'complete',
            record_counts=json.dumps({'files': file_count, 'mode': mode}),
        )
        # Watermarks move only now — a failed run must not shrink the next window.
        ports.commit_watermarks(mode, trigger_type)
        logger.info('[sync-phase] run_id=%s phase=complete mode=%s files=%d',
                    run_id, mode, file_count)

        return {
            'mode': mode,
            'run_id': run_id,
            'state': 'complete',
            'file_count': file_count,
        }

    except Exception as exc:
        error_msg = str(exc)
        logger.exception(
            '[sync-failed] run_id=%s mode=%s trigger=%s project=%s phase=%s error=%s',
            run_id, mode, trigger_type, bare_project_id, sync_phase, error_msg,
        )
        ports.update_run(run_id, 'failed', error=error_msg, phase=sync_phase)
        raise
