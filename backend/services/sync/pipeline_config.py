
import logging

from backend.clients.databricks_client import DatabricksClient
from backend.services.sync.sync_config import pipeline_test_subset_conf

logger = logging.getLogger(__name__)


PIPELINE_RETRY_TUNING: dict[str, str] = {
    'pipelines.numUpdateRetryAttempts': '2',
    'pipelines.maxFlowRetryAttempts':   '1',
}

# Pipeline B only — batch @dlt.view sources feeding create_auto_cdc_flow(once=True)
# fail the incompatible-view check unless this is disabled.
CDC_PIPELINE_TUNING: dict[str, str] = {
    **PIPELINE_RETRY_TUNING,
    'pipelines.incompatibleViewCheck.enabled': 'false',
}


def build_base_pipeline_conf(catalog_name: str, bare_project_id: str) -> dict:
    """Stable pipeline keys shared across snapshot and CDC runs."""
    return {
        'acc.dc_project_id': bare_project_id,
        'acc.catalog':       catalog_name or '',
        'acc.volume_layout': 'legacy',
        'acc.test_mode':     'false',
        **pipeline_test_subset_conf(),
    }


def build_snapshot_pipeline_conf(
    catalog_name: str, bare_project_id: str, **extra: str,
) -> dict:
    """Per-run config for Pipeline A (snapshot AUTO CDC FROM SNAPSHOT)."""
    return {
        **build_base_pipeline_conf(catalog_name, bare_project_id),
        **PIPELINE_RETRY_TUNING,
        **extra,
    }


def build_cdc_pipeline_conf(
    catalog_name: str, bare_project_id: str, **extra: str,
) -> dict:
    """Per-run config for Pipeline B (Delta CDC / create_auto_cdc_flow)."""
    return {
        **build_base_pipeline_conf(catalog_name, bare_project_id),
        **CDC_PIPELINE_TUNING,
        **extra,
    }


def apply_and_start_pipeline_update(
    dbx: DatabricksClient,
    pipeline_id: str,
    configuration: dict,
    *,
    label: str = 'pipeline',
    full_refresh: bool = False,
) -> str:
    """Merge configuration into the pipeline resource, then trigger an update.

    Lakeflow notebooks read ``spark.conf.get('acc.*')`` from the pipeline
    definition; per-update configuration alone is not always sufficient.

    Pipeline B (legacy CDC): stream root is stable at the project parent
    (.../data_connector_cdc/{project_id}/) with recursiveFileLookup, so
    Sync CDC uses ``full_refresh=False`` (incremental merge). Full Refresh CDC
    and snapshot baseline runs use ``full_refresh=True``.
    """
    logger.info(
        '[pipeline-start] label=%s pipeline_id=%s full_refresh=%s dc_snapshot=%s dc_cdc=%s',
        label,
        pipeline_id,
        full_refresh,
        configuration.get('acc.dc_snapshot_path', ''),
        configuration.get('acc.dc_cdc_path', ''),
    )
    dbx.apply_pipeline_configuration(pipeline_id, configuration)
    update_id = dbx.start_pipeline_update(
        pipeline_id,
        configuration=configuration,
        full_refresh=full_refresh,
    )
    logger.info(
        '[pipeline-start] started label=%s update_id=%s pipeline_id=%s full_refresh=%s',
        label,
        update_id,
        pipeline_id,
        full_refresh,
    )
    return update_id


def sync_project_pipeline_defaults(
    dbx: DatabricksClient,
    bs_state: dict,
    bare_project_id: str,
) -> None:
    """Persist catalog + project id on both bronze pipelines after ACC project save."""
    catalog_name = (bs_state.get('catalog_name') or '').strip()
    if not catalog_name or not bare_project_id:
        logger.warning(
            'Skipping pipeline default config sync — catalog=%r project=%r',
            catalog_name,
            bare_project_id,
        )
        return

    base_conf = build_base_pipeline_conf(catalog_name, bare_project_id)
    for label, pipeline_id in (
        ('snapshot', bs_state.get('snapshot_pipeline_id')),
        ('CDC', bs_state.get('cdc_pipeline_id')),
    ):
        if not pipeline_id:
            continue
        conf = (
            {**base_conf, **CDC_PIPELINE_TUNING}
            if label == 'CDC'
            else base_conf
        )
        logger.info(
            'Syncing ACC project defaults to %s pipeline %s',
            label,
            pipeline_id,
        )
        dbx.apply_pipeline_configuration(pipeline_id, conf)
