"""
Purpose: The workspace bootstrap sequence, once, for both auth paths. Validates the workspace
and SQL warehouse, verifies the Unity Catalog, creates the bronze schema/volume/_meta_ tables,
uploads the notebooks and creates the two pipelines plus the sync workflows.

Identity-free by design: the caller supplies an authenticated DatabricksClient and a
``progress(step, message, *, error=None)`` callback, and gets the artefact ids back. The U2M
driver keys progress and persistence on user_id, the M2M driver on connection_id, and neither
duplicates the steps. Extracted from bootstrap_service._provision_catalog — behaviour, call
order and progress strings are deliberately unchanged.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

from backend.clients.databricks_client import DOWNLOAD_ROLE_CDC, DatabricksClient
from backend.services import pipeline_naming
from backend.services.sync.pipeline_config import CDC_PIPELINE_TUNING
from backend.services.sync.sync_config import is_sync_test_subset_enabled

logger = logging.getLogger(__name__)

# provisioning -> services -> backend -> acc-connector
_APP_ROOT = Path(__file__).resolve().parents[3]
NOTEBOOKS_DIR = _APP_ROOT / 'notebooks'
PK_CONFIG_FILE = _APP_ROOT / 'config' / 'pk_config_template.json'

SCHEMA_BRONZE = 'bronze'
VOLUME_NAME = 'acc_bronze_volume'

# Registry / status tables live under <catalog>.bronze with a `_meta_` prefix — enough
# namespacing without provisioning a separate UC schema. Kept in sync with
# notebooks/auto_cdc_pipeline.py.
META_REGISTRY_TABLE = '_meta_bronze_pk_registry'
META_STATUS_TABLE = '_meta_bronze_table_status'
META_SCHEMA_VERSIONS_TABLE = '_meta_bronze_schema_versions'

NOTEBOOK_NAMES = (
    'acc_snapshot_pipeline',
    'acc_delta_cdc_pipeline',
    'acc_pipeline_test',
    'auto_cdc_pipeline',
    'auto_cdc_cdc_pipeline',
    'bulk_downloader',
    'seed_registry',
)


def notebook_download_enabled() -> bool:
    return os.getenv('ENABLE_NOTEBOOK_DOWNLOAD', 'false').lower() == 'true'


def volume_path_for(catalog_name: str) -> str:
    return f'/Volumes/{catalog_name}/{SCHEMA_BRONZE}/{VOLUME_NAME}'


def _fail(progress, step: int, message: str, exc: Exception, label: str):
    progress(step, '', error=f'{message}: {exc}')
    raise RuntimeError(f'{label} failed: {exc}')


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def validate_workspace(dbx: DatabricksClient, progress) -> None:
    progress(1, 'Validating Databricks workspace access...')
    try:
        dbx.get('/api/2.0/clusters/spark-versions')
    except Exception as e:
        _fail(progress, 1, 'Workspace validation failed', e, 'Step 1')


def ensure_warehouse_running(dbx: DatabricksClient, progress) -> str:
    """Pick a SQL warehouse and wait for it to be RUNNING.

    The wait matters: DDL sent to a STARTING warehouse times out, and a cold start can take
    several minutes.
    """
    progress(2, 'Detecting SQL Warehouse...')
    warehouse_id = None
    try:
        wh_resp = dbx.get('/api/2.0/sql/warehouses')
        warehouses = wh_resp.get('warehouses', [])
        running = [w for w in warehouses if w.get('state') == 'RUNNING']
        chosen = running[0] if running else (warehouses[0] if warehouses else None)
        if chosen:
            warehouse_id = chosen['id']
            progress(2, f'SQL Warehouse detected: {chosen.get("name")} ({warehouse_id})')
        else:
            progress(2, 'No SQL Warehouse found')
    except Exception as e:
        progress(2, f'Warehouse detection warning: {e}')

    if not warehouse_id:
        progress(2, '', error='No SQL Warehouse available. Please create one in Databricks SQL.')
        raise RuntimeError('Step 2 failed: No SQL Warehouse available for DDL execution')

    progress(2, 'Waiting for SQL Warehouse to be RUNNING (cold start may take ~5 min)…')
    try:
        dbx.wait_for_warehouse(warehouse_id)
        progress(2, f'SQL Warehouse is RUNNING: {warehouse_id}')
    except Exception as e:
        _fail(progress, 2, 'Warehouse did not become ready', e, 'Step 2')
    return warehouse_id


def validate_metastore(dbx: DatabricksClient, warehouse_id: str, progress) -> str:
    progress(3, 'Validating Unity Catalog and metastore...')
    try:
        result = dbx.execute_sql(warehouse_id, 'SELECT current_metastore()')
        data_array = result.get('result', {}).get('data_array', [])
        metastore_name = data_array[0][0] if data_array else 'unknown'
        progress(3, f'Unity Catalog confirmed — metastore: {metastore_name}')
        return metastore_name
    except Exception as e:
        _fail(progress, 3, 'Unity Catalog not available', e, 'Step 3')


def verify_catalog(dbx: DatabricksClient, catalog_name: str, progress) -> None:
    progress(4, f'Verifying catalog: {catalog_name}...')
    try:
        dbx.uc_verify_catalog_exists(catalog_name)
        progress(4, f'Catalog verified: {catalog_name}')
    except Exception as e:
        _fail(progress, 4, 'Catalog not found or not accessible', e, 'Step 4')


def ensure_bronze_schema(dbx: DatabricksClient, catalog_name: str, progress) -> None:
    progress(5, 'Creating schema: bronze...')
    try:
        dbx.uc_create_schema(catalog_name, SCHEMA_BRONZE)
        progress(5, f'Schema ready: {catalog_name}.bronze')
    except Exception as e:
        _fail(progress, 5, 'Schema creation failed', e, 'Step 5')


def ensure_volume(dbx: DatabricksClient, catalog_name: str, progress) -> str:
    progress(6, f'Creating volume: {catalog_name}.{SCHEMA_BRONZE}.{VOLUME_NAME}...')
    try:
        dbx.uc_create_volume(catalog_name, SCHEMA_BRONZE, VOLUME_NAME)
        progress(6, f'Volume ready: {VOLUME_NAME}')
    except Exception as e:
        _fail(progress, 6, 'Volume creation failed', e, 'Step 6')
    return volume_path_for(catalog_name)


def validate_volume_path(dbx: DatabricksClient, volume_path: str, progress) -> None:
    progress(7, f'Validating volume path: {volume_path}...')
    if not dbx.validate_volume_path(volume_path):
        # A freshly created volume can take a moment to appear on the Files API.
        time.sleep(3)
        if not dbx.validate_volume_path(volume_path):
            err = f'Volume path {volume_path} not accessible via Files API'
            progress(7, '', error=err)
            raise RuntimeError(f'Step 7 failed: {err}')
    progress(7, f'Volume path confirmed: {volume_path}')


def create_meta_tables(dbx: DatabricksClient, warehouse_id: str, catalog_name: str) -> None:
    """CREATE TABLE IF NOT EXISTS for the three `_meta_*` tables. Seeds no rows — the PK
    registry is populated from schema.json on the volume during the first sync.

    ``schema`` and ``table`` are reserved Spark SQL keywords, hence the backticks.
    """
    fqn_registry = f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_REGISTRY_TABLE}`'
    fqn_status = f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_STATUS_TABLE}`'
    fqn_schema_versions = f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_SCHEMA_VERSIONS_TABLE}`'

    dbx.execute_sql(warehouse_id, (
        f'CREATE TABLE IF NOT EXISTS {fqn_registry} ('
        '  catalog     STRING NOT NULL, '
        '  `schema`    STRING NOT NULL, '
        '  `table`     STRING NOT NULL, '
        '  pk_columns  ARRAY<STRING> NOT NULL, '
        '  locked_at   TIMESTAMP NOT NULL, '
        '  source      STRING NOT NULL'
        ') USING DELTA'
    ))

    dbx.execute_sql(warehouse_id, (
        f'CREATE TABLE IF NOT EXISTS {fqn_status} ('
        '  catalog                STRING NOT NULL, '
        '  `schema`               STRING NOT NULL, '
        '  `table`                STRING NOT NULL, '
        '  last_synced_at         TIMESTAMP NOT NULL, '
        '  last_run_status        STRING NOT NULL, '
        '  last_error_message     STRING, '
        '  csv_header_at_last_run ARRAY<STRING>, '
        '  column_diff            STRUCT<'
        '                           added: ARRAY<STRING>, '
        '                           absent_in_csv: ARRAY<STRING>, '
        '                           type_widened: ARRAY<STRING>, '
        '                           type_kept_old: ARRAY<STRING>'
        '                         >'
        ') USING DELTA'
    ))

    dbx.execute_sql(warehouse_id, (
        f'CREATE TABLE IF NOT EXISTS {fqn_schema_versions} ('
        '  catalog          STRING NOT NULL, '
        '  project_id       STRING NOT NULL, '
        '  config_type      STRING NOT NULL, '
        '  config_hash      STRING NOT NULL, '
        '  last_applied_at  TIMESTAMP NOT NULL, '
        '  config_path      STRING'
        ') USING DELTA'
    ))


def ensure_meta_tables(dbx: DatabricksClient, warehouse_id: str, catalog_name: str,
                       progress) -> None:
    progress(8, 'Creating PK registry / status tables...')
    try:
        create_meta_tables(dbx, warehouse_id, catalog_name)
        progress(
            8,
            'PK registry ready — empty until first sync seeds it from '
            'autodesk_data_extract.zip',
        )
    except Exception as e:
        _fail(progress, 8, '_meta_* table creation failed', e, 'Step 8')


def upload_pk_config(dbx: DatabricksClient, volume_path: str, progress) -> None:
    progress(8, 'Uploading pk_config_template.json to Volume...')
    try:
        if not PK_CONFIG_FILE.exists():
            raise FileNotFoundError(f'pk_config_template.json not found: {PK_CONFIG_FILE}')
        content = PK_CONFIG_FILE.read_text()
        target = f'{volume_path}/pk_config.json'
        dbx.put_file(target, content.encode())
        logger.info('Uploaded pk_config.json to %s', target)
        progress(8, 'pk_config.json uploaded to Volume')
    except Exception as e:
        _fail(progress, 8, 'pk_config upload failed', e, 'Step 8.5')


def upload_notebooks(dbx: DatabricksClient, progress) -> str:
    """Upload the pipeline notebooks and their shared helpers; returns the folder."""
    progress(9, 'Uploading notebooks to Databricks workspace...')
    try:
        user_email = dbx.get_current_user_email()
        notebook_folder = f'/Users/{user_email}/acc/v1'
        dbx.mkdirs(notebook_folder)

        for nb_name in NOTEBOOK_NAMES:
            nb_file = NOTEBOOKS_DIR / f'{nb_name}.py'
            if not nb_file.exists():
                raise FileNotFoundError(f'Notebook file not found: {nb_file}')
            content_b64 = base64.b64encode(nb_file.read_bytes()).decode()
            dbx.import_notebook(f'{notebook_folder}/{nb_name}', content_b64)
            logger.info('Uploaded notebook: %s/%s', notebook_folder, nb_name)

        shared_dir = NOTEBOOKS_DIR / 'shared'
        if shared_dir.is_dir():
            dbx.mkdirs(f'{notebook_folder}/shared')
            for shared_file in sorted(shared_dir.glob('*.py')):
                content_b64 = base64.b64encode(shared_file.read_bytes()).decode()
                dbx.import_notebook(
                    f'{notebook_folder}/shared/{shared_file.stem}', content_b64,
                )
                logger.info('Uploaded shared notebook: %s/shared/%s',
                            notebook_folder, shared_file.stem)

        progress(9, f'Notebooks uploaded to {notebook_folder}')
        return notebook_folder
    except Exception as e:
        _fail(progress, 9, 'Notebook upload failed', e, 'Step 9')


def ensure_pipelines(dbx: DatabricksClient, catalog_name: str, notebook_folder: str,
                     names: dict, progress) -> tuple[str, str]:
    """Create (or reuse) Pipeline A (snapshot) and Pipeline B (CDC).

    Serverless requires the ADVANCED edition — the Pipelines API rejects serverless=true on
    PRO with INVALID_PARAMETER_VALUE.
    """
    progress(10, 'Creating Databricks Snapshot Pipeline...')
    try:
        snapshot_pipeline_config = {
            'name': names['snapshot_pipeline'],
            'edition': 'ADVANCED',
            'catalog': catalog_name,
            'target': SCHEMA_BRONZE,
            'continuous': False,
            'serverless': True,
            'libraries': [{
                'notebook': {'path': f'{notebook_folder}/acc_snapshot_pipeline'},
            }],
            # Surfaced to the notebook as spark.conf.get('acc.catalog') so one notebook
            # drives any tenant's catalog.
            'configuration': {
                'acc.catalog': catalog_name,
                'acc.volume_layout': 'legacy',
                'acc.test_mode': 'false',
            },
        }
        snapshot_pipeline_id = dbx.get_or_create_pipeline(
            names['snapshot_pipeline'], snapshot_pipeline_config,
        )
        progress(10, f'Pipeline ready — {names["snapshot_pipeline"]}: {snapshot_pipeline_id}')
    except Exception as e:
        _fail(progress, 10, 'Pipeline creation failed', e, 'Step 10')

    progress(10, 'Creating Databricks CDC pipeline...')
    try:
        cdc_pipeline_config = {
            'name': names['cdc_pipeline'],
            'edition': 'ADVANCED',
            'catalog': catalog_name,
            'target': SCHEMA_BRONZE,
            'continuous': False,
            'serverless': True,
            'libraries': [{
                'notebook': {'path': f'{notebook_folder}/acc_delta_cdc_pipeline'},
            }],
            'configuration': {
                'acc.catalog': catalog_name,
                'acc.volume_layout': 'legacy',
                'acc.test_mode': 'false',
                **CDC_PIPELINE_TUNING,
            },
        }
        cdc_pipeline_id = dbx.get_or_create_pipeline(
            names['cdc_pipeline'], cdc_pipeline_config,
        )
        progress(
            10,
            f'Pipelines ready — snapshot: {snapshot_pipeline_id}, CDC: {cdc_pipeline_id}',
        )
    except Exception as e:
        _fail(progress, 10, 'CDC pipeline creation failed', e, 'Step 10b')

    return snapshot_pipeline_id, cdc_pipeline_id


def ensure_sync_workflows(dbx: DatabricksClient, names: dict, notebook_folder: str,
                          snapshot_pipeline_id: str, cdc_pipeline_id: str,
                          progress) -> tuple[int | None, int | None]:
    """Create the download → pipeline workflows used by the notebook download path."""
    progress(10, 'Creating snapshot + CDC sync workflows...')
    try:
        nb_path = f'{notebook_folder}/bulk_downloader'
        snapshot_workflow_id, _ = dbx.create_combined_snapshot_workflow(
            names['snapshot_workflow'],
            snapshot_pipeline_id,
            cdc_pipeline_id,
            download_notebook_path=nb_path,
            replace=True,
        )
        cdc_workflow_id, _ = dbx.create_sync_workflow(
            names['cdc_workflow'],
            nb_path,
            cdc_pipeline_id,
            download_role=DOWNLOAD_ROLE_CDC,
            full_refresh=is_sync_test_subset_enabled(),
            replace=True,
        )
        progress(
            10,
            f'Workflows ready — snapshot: {snapshot_workflow_id}, CDC: {cdc_workflow_id}',
        )
        return snapshot_workflow_id, cdc_workflow_id
    except Exception as e:
        _fail(progress, 10, 'Sync workflow creation failed', e, 'Step 10c')


# ---------------------------------------------------------------------------
# The whole sequence
# ---------------------------------------------------------------------------


def run_all(
    dbx: DatabricksClient,
    catalog_name: str,
    *,
    progress,
    create_workflows: bool | None = None,
) -> dict:
    """Steps 1–10c. Returns the artefact ids; persisting them is the driver's job.

    ``create_workflows=None`` follows ENABLE_NOTEBOOK_DOWNLOAD, which is what the U2M path
    has always done. The M2M driver passes True unconditionally: flipping that flag on a
    fleet would otherwise strand every already-bootstrapped connection at "re-run bootstrap".
    """
    volume_path = volume_path_for(catalog_name)
    names = pipeline_naming.artifact_names(catalog_name)

    progress(0, 'Starting bootstrap...')
    validate_workspace(dbx, progress)
    warehouse_id = ensure_warehouse_running(dbx, progress)
    validate_metastore(dbx, warehouse_id, progress)
    verify_catalog(dbx, catalog_name, progress)
    ensure_bronze_schema(dbx, catalog_name, progress)
    ensure_volume(dbx, catalog_name, progress)
    validate_volume_path(dbx, volume_path, progress)
    ensure_meta_tables(dbx, warehouse_id, catalog_name, progress)
    upload_pk_config(dbx, volume_path, progress)
    notebook_folder = upload_notebooks(dbx, progress)

    nb_download = notebook_download_enabled()
    if not nb_download:
        progress(9, 'Notebook download disabled — sync uses Flask volume upload path')

    snapshot_pipeline_id, cdc_pipeline_id = ensure_pipelines(
        dbx, catalog_name, notebook_folder, names, progress,
    )

    snapshot_workflow_id = None
    cdc_workflow_id = None
    if create_workflows or (create_workflows is None and nb_download):
        snapshot_workflow_id, cdc_workflow_id = ensure_sync_workflows(
            dbx, names, notebook_folder, snapshot_pipeline_id, cdc_pipeline_id, progress,
        )

    return {
        'snapshot_pipeline_id': snapshot_pipeline_id,
        'cdc_pipeline_id': cdc_pipeline_id,
        'snapshot_workflow_id': snapshot_workflow_id,
        'cdc_workflow_id': cdc_workflow_id,
        'notebook_folder': notebook_folder,
        'volume_path': volume_path,
        'compute_type': 'sql_warehouse',
        'warehouse_id': warehouse_id,
        'catalog_name': catalog_name,
        'pipeline_names': names,
    }
