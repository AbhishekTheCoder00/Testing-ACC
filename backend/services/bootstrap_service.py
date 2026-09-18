"""
bootstrap.py — One-time workspace provisioning for the ACC Connector.

12-step bootstrap sequence:
  1.  Validate Databricks workspace access
  2.  Detect SQL Warehouse (required — bootstrap fails without one)
  3.  Validate Unity Catalog / metastore
  4.  Verify user-selected Unity Catalog exists (must be created in Databricks first)
  5.  CREATE SCHEMA IF NOT EXISTS <catalog>.bronze
  6.  CREATE VOLUME IF NOT EXISTS <catalog>.bronze.acc_bronze_volume
  7.  Validate volume path is accessible via Files API
  8.  CREATE TABLE IF NOT EXISTS for the two _meta_* registry/status tables.
      The PK rows are NOT seeded here — the real schema.json arrives only
      after the first sync, inside autodesk_data_extract.zip in the volume.
      sync_service seeds the registry on each run from that zip.
  9.  Upload notebooks to /Users/{email}/acc/v1/
  9b. Create Forma bulk-downloader Job (get_or_create by name). Used by
      sync_service when ENABLE_NOTEBOOK_DOWNLOAD is set — moves the DC
      signed-URL download out of the Flask process and into the customer's
      workspace.
  10. Create bronze AUTO CDC FROM SNAPSHOT pipeline (get_or_create by name)
  11. Store job IDs + config in SQLite

Bootstrap is idempotent — safe to re-run.
"""
import base64
import logging
import os
import re
import threading
from pathlib import Path
from backend.clients.databricks_client import DatabricksClient, DOWNLOAD_ROLE_CDC
from backend.repositories import state_store as db
from backend.services import pipeline_naming
from backend.services.provisioning import bootstrap_steps
from backend.services.sync.pipeline_config import CDC_PIPELINE_TUNING
from backend.services.sync.sync_config import is_sync_test_subset_enabled
# from backend.services.sync.schema_service import _load_schema_doc_from_api
logger = logging.getLogger(__name__)


def use_shared_steps() -> bool:
    """Whether bootstrap runs through the shared step library (ADR.md, Phase 1).

    Temporary. Defaults to false so the extraction is opt-in; retired together with
    _provision_catalog_legacy once staging has soaked one snapshot and one CDC cycle.
    """
    return os.getenv('USE_SHARED_STEPS', 'false').lower() == 'true'
# Registry / status table names live under <catalog>.bronze with a `_meta_`
# prefix — sufficient namespacing without provisioning a separate UC schema.
# Kept in sync with notebooks/auto_cdc_pipeline.py and sync_service.py.

# UC catalog names: letters, digits, underscore; must start with letter or underscore
_UC_CATALOG_NAME_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]{0,127}$')

# Bronze ingestion is now a Lakeflow Spark Declarative Pipeline using
# AUTO CDC FROM SNAPSHOT (notebooks/auto_cdc_pipeline.py). Requires Pro,
# Advanced, or Serverless workspace edition — Standard returns 400 from
# POST /api/2.0/pipelines and bootstrap surfaces the error verbatim.

# Bulk downloader Job. Runs notebooks/bulk_downloader.py against a manifest
# of DC signed URLs that sync_service writes into UC Volume. Forma-branded
# to align with the umbrella connector identity — the existing bronze
# pipeline keeps its ACC-prefixed name for backward compatibility until a
# coordinated rename release.

# NOTEBOOKS_DIR = Path(__file__).parent.parent / 'notebooks'
NOTEBOOKS_DIR = Path(__file__).parent.parent.parent / 'notebooks'
# Progress is stored here so /bootstrap/status can return live updates
_bootstrap_progress: dict = {}   # user_id → {step, message, done, error}
_bootstrap_in_flight: set[str] = set()
_bootstrap_in_flight_lock = threading.Lock()

SCHEMA_BRONZE = 'bronze'
VOLUME_NAME   = 'acc_bronze_volume'

# Registry / status table names live under <catalog>.bronze with a `_meta_`
# prefix — sufficient namespacing without provisioning a separate UC schema.
# Kept in sync with notebooks/auto_cdc_pipeline.py and sync_service.py.
META_REGISTRY_TABLE         = '_meta_bronze_pk_registry'
META_STATUS_TABLE           = '_meta_bronze_table_status'
META_SCHEMA_VERSIONS_TABLE  = '_meta_bronze_schema_versions'


def get_progress(user_id: str) -> dict:
    return _bootstrap_progress.get(user_id, {'step': 0, 'message': 'Not started', 'done': False})


def try_begin_bootstrap(user_id: str) -> bool:
    """Mark bootstrap in-flight for this user. Returns False if one is already running."""
    with _bootstrap_in_flight_lock:
        if user_id in _bootstrap_in_flight:
            return False
        _bootstrap_in_flight.add(user_id)
        return True


def end_bootstrap(user_id: str) -> None:
    """Clear the in-flight marker so the user can start another bootstrap."""
    with _bootstrap_in_flight_lock:
        _bootstrap_in_flight.discard(user_id)


def _set_progress(user_id: str, step: int, message: str, done: bool = False, error: str = None):
    _bootstrap_progress[user_id] = {
        'step':    step,
        'message': message,
        'done':    done or bool(error),
        'error':   error,
    }
    if error:
        logger.error('Bootstrap step %d failed for user %s: %s', step, user_id, error)
    else:
        logger.info('Bootstrap step %d [user %s]: %s', step, user_id, message)


def fail_bootstrap(user_id: str, message: str) -> None:
    """Mark bootstrap failed so /bootstrap/status stops polling with an error."""
    prog = get_progress(user_id)
    if prog.get('error'):
        return
    _set_progress(
        user_id,
        prog.get('step', 0),
        prog.get('message', ''),
        error=message,
    )


def validate_uc_catalog_name(name: str) -> str:
    """Return stripped catalog name or raise ValueError."""
    catalog_name = (name or '').strip()
    if not catalog_name or not _UC_CATALOG_NAME_RE.match(catalog_name):
        raise ValueError(
            'Invalid Unity Catalog name. Use letters, digits, underscore; max 128 characters.'
        )
    return catalog_name

def _create_meta_tables(
    dbx: DatabricksClient,
    warehouse_id: str,
    catalog_name: str,
) -> None:
    """CREATE TABLE IF NOT EXISTS for the two _meta_* tables.

    Idempotent. Does NOT seed any rows — the per-tenant PK registry is
    populated by the Bronze/CDC pipeline notebooks from the canonical
    ``schema.json`` on the volume (hash-gated). The bundled schema.json
    under acc-connector/schemas/ is documentation only and is never read
    at runtime.

    `schema` and `table` are reserved Spark SQL keywords; backticks are
    required in DDL and DML wherever they appear as identifiers.
    """
    fqn_registry = (
        f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_REGISTRY_TABLE}`'
    )
    fqn_status = (
        f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_STATUS_TABLE}`'
    )
    fqn_schema_versions = (
        f'`{catalog_name}`.`{SCHEMA_BRONZE}`.`{META_SCHEMA_VERSIONS_TABLE}`'
    )

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





def run_bootstrap(user_id: str, workspace_url: str, pat: str,
                  catalog_name: str, *, claim_id: int) -> dict:
    """Provision a catalog using a claim already taken by ``bootstrap_start``.

    Claiming happens synchronously in the route so conflicts can return 409.
    This worker only validates the claim is still active, then provisions.
    Release on failure is handled by the route's background thread.
    """
    catalog_name = validate_uc_catalog_name(catalog_name)
    workspace_key = db.normalize_workspace_key(workspace_url)
    claim = db.get_claim(claim_id)
    if not claim or claim.get('released_at') is not None:
        raise RuntimeError(f'Catalog claim {claim_id} is no longer active')
    if claim['owner_user_id'] != user_id:
        raise RuntimeError('Catalog claim belongs to another user')
    if claim['catalog_name'] != catalog_name:
        raise RuntimeError('Catalog claim does not match requested catalog')
    if claim['workspace_key'] != workspace_key:
        raise RuntimeError('Catalog claim workspace mismatch')
    return _provision_catalog(user_id, workspace_url, pat, catalog_name, claim)


def _provision_catalog(user_id: str, workspace_url: str, pat: str,
                       catalog_name: str, claim: dict) -> dict:
    """Dispatch to the shared step library or the pre-refactor path.

    ``USE_SHARED_STEPS`` exists so the extraction can be rolled back at runtime. Both
    branches must behave identically; ``tests/test_bootstrap_steps.py`` asserts that by
    recording every Databricks call and progress message from each and comparing them.
    Delete this switch and ``_provision_catalog_legacy`` once the staging soak passes —
    see ADR.md (Phase 1).
    """
    if use_shared_steps():
        return _provision_catalog_shared(user_id, workspace_url, pat, catalog_name, claim)
    return _provision_catalog_legacy(user_id, workspace_url, pat, catalog_name, claim)


def _persist_bootstrap(user_id: str, dbx: DatabricksClient, catalog_name: str,
                       claim: dict, result: dict) -> dict:
    """Step 11 — store the artefact ids and push the project defaults onto the pipelines.

    This is the part that genuinely differs between the two auth paths (user-keyed rows vs
    connection-keyed rows), which is why it lives in the driver and not in the step library.
    """
    names = result['pipeline_names']
    db.save_bootstrap_state(
        user_id=user_id,
        snapshot_pipeline_id=result['snapshot_pipeline_id'],
        cdc_pipeline_id=result['cdc_pipeline_id'],
        notebook_folder=result['notebook_folder'],
        volume_path=result['volume_path'],
        compute_type=result['compute_type'],
        warehouse_id=result['warehouse_id'],
        catalog_name=catalog_name,
        snapshot_workflow_id=result['snapshot_workflow_id'],
        cdc_workflow_id=result['cdc_workflow_id'],
        catalog_claim_id=claim['claim_id'],
    )
    db.update_claim_artifacts(
        claim['claim_id'],
        snapshot_pipeline_id=result['snapshot_pipeline_id'],
        cdc_pipeline_id=result['cdc_pipeline_id'],
        snapshot_pipeline_name=names['snapshot_pipeline'],
        cdc_pipeline_name=names['cdc_pipeline'],
        snapshot_workflow_id=result['snapshot_workflow_id'],
        cdc_workflow_id=result['cdc_workflow_id'],
    )

    acc_cfg = db.get_acc_config(user_id)
    if acc_cfg and acc_cfg.get('project_id'):
        bare_pid = acc_cfg['project_id']
        if bare_pid.startswith('b.'):
            bare_pid = bare_pid[2:]
        try:
            from backend.services.sync.pipeline_config import sync_project_pipeline_defaults
            sync_project_pipeline_defaults(
                dbx,
                {
                    'catalog_name': catalog_name,
                    'snapshot_pipeline_id': result['snapshot_pipeline_id'],
                    'cdc_pipeline_id': result['cdc_pipeline_id'],
                },
                bare_pid,
            )
        except Exception as exc:
            logger.warning('Bootstrap: pipeline default config sync failed: %s', exc)

    out = dict(result)
    out['catalog_claim_id'] = claim['claim_id']
    return out


def _provision_catalog_shared(user_id: str, workspace_url: str, pat: str,
                              catalog_name: str, claim: dict) -> dict:
    """U2M driver over the shared step library — owns only identity and persistence."""
    dbx = DatabricksClient(workspace_url, pat)

    def progress(step: int, message: str, *, error: str = None) -> None:
        _set_progress(user_id, step, message, error=error)

    result = bootstrap_steps.run_all(dbx, catalog_name, progress=progress)
    _set_progress(user_id, 11, 'Saving bootstrap configuration...')
    out = _persist_bootstrap(user_id, dbx, catalog_name, claim, result)
    _set_progress(user_id, 11, 'Bootstrap complete!', done=True)
    return out


def _provision_catalog_legacy(user_id: str, workspace_url: str, pat: str,
                              catalog_name: str, claim: dict) -> dict:
    """
    Execute the full 10-step bootstrap sequence for an existing Unity Catalog.
    Returns a summary dict. Raises on fatal errors.

    PRE-REFACTOR PATH — kept only until USE_SHARED_STEPS is retired. Any change here must
    also be made in backend/services/provisioning/bootstrap_steps.py, which is the copy the
    M2M path uses and the copy that survives.
    """
    volume_path = f'/Volumes/{catalog_name}/{SCHEMA_BRONZE}/{VOLUME_NAME}'
    names = pipeline_naming.artifact_names(catalog_name)

    dbx = DatabricksClient(workspace_url, pat)
    _set_progress(user_id, 0, 'Starting bootstrap...')

    # ------------------------------------------------------------------
    # Step 1 — Validate workspace access
    # ------------------------------------------------------------------
    _set_progress(user_id, 1, 'Validating Databricks workspace access...')
    try:
        dbx.get('/api/2.0/clusters/spark-versions')
    except Exception as e:
        _set_progress(user_id, 1, '', error=f'Workspace validation failed: {e}')
        raise RuntimeError(f'Step 1 failed: {e}')

    # ------------------------------------------------------------------
    # Step 2 — Detect compute
    # ------------------------------------------------------------------
    _set_progress(user_id, 2, 'Detecting SQL Warehouse...')
    warehouse_id  = None
    compute_type  = 'sql_warehouse'
    try:
        wh_resp = dbx.get('/api/2.0/sql/warehouses')
        warehouses = wh_resp.get('warehouses', [])
        # Prefer a running warehouse; fall back to first available
        running = [w for w in warehouses if w.get('state') == 'RUNNING']
        chosen  = running[0] if running else (warehouses[0] if warehouses else None)
        if chosen:
            warehouse_id = chosen['id']
            compute_type = 'sql_warehouse'
            _set_progress(user_id, 2, f'SQL Warehouse detected: {chosen.get("name")} ({warehouse_id})')
        else:
            _set_progress(user_id, 2, 'No SQL Warehouse found')
    except Exception as e:
        _set_progress(user_id, 2, f'Warehouse detection warning: {e}')

    if not warehouse_id:
        # Cannot execute DDL without a SQL warehouse — surface a clear error
        _set_progress(user_id, 2, '', error='No SQL Warehouse available. Please create one in Databricks SQL.')
        raise RuntimeError('Step 2 failed: No SQL Warehouse available for DDL execution')

    # Wait for the warehouse to be fully RUNNING before sending DDL.
    # A cold-start warehouse can take 3-8 minutes; skipping this causes SQL
    # statements to time out while the warehouse is still in STARTING state.
    _set_progress(user_id, 2, f'Waiting for SQL Warehouse to be RUNNING (cold start may take ~5 min)…')
    try:
        dbx.wait_for_warehouse(warehouse_id)
        _set_progress(user_id, 2, f'SQL Warehouse is RUNNING: {warehouse_id}')
    except Exception as e:
        _set_progress(user_id, 2, '', error=f'Warehouse did not become ready: {e}')
        raise RuntimeError(f'Step 2 failed: {e}')

    # ------------------------------------------------------------------
    # Step 3 — Validate Unity Catalog / metastore
    # ------------------------------------------------------------------
    _set_progress(user_id, 3, 'Validating Unity Catalog and metastore...')
    try:
        result = dbx.execute_sql(warehouse_id, 'SELECT current_metastore()')
        data_array = result.get('result', {}).get('data_array', [])
        metastore_name = data_array[0][0] if data_array else 'unknown'
        _set_progress(user_id, 3, f'Unity Catalog confirmed — metastore: {metastore_name}')
    except Exception as e:
        _set_progress(user_id, 3, '', error=f'Unity Catalog not available: {e}')
        raise RuntimeError(f'Step 3 failed: {e}')

    # ------------------------------------------------------------------
    # Step 4 — Verify catalog exists (created by admin / user in Databricks UI)
    # ------------------------------------------------------------------
    _set_progress(user_id, 4, f'Verifying catalog: {catalog_name}...')
    try:
        dbx.uc_verify_catalog_exists(catalog_name)
        _set_progress(user_id, 4, f'Catalog verified: {catalog_name}')
    except Exception as e:
        _set_progress(user_id, 4, '', error=f'Catalog not found or not accessible: {e}')
        raise RuntimeError(f'Step 4 failed: {e}')

    # ------------------------------------------------------------------
    # Step 5 — Create the bronze schema (UC REST API). Silver was removed.
    # ------------------------------------------------------------------
    _set_progress(user_id, 5, 'Creating schema: bronze...')
    try:
        dbx.uc_create_schema(catalog_name, SCHEMA_BRONZE)
        _set_progress(user_id, 5, f'Schema ready: {catalog_name}.bronze')
    except Exception as e:
        _set_progress(user_id, 5, '', error=f'Schema creation failed: {e}')
        raise RuntimeError(f'Step 5 failed: {e}')

    # ------------------------------------------------------------------
    # Step 6 — Create volume (UC REST API)
    # ------------------------------------------------------------------
    _set_progress(user_id, 6, f'Creating volume: {catalog_name}.{SCHEMA_BRONZE}.{VOLUME_NAME}...')
    try:
        dbx.uc_create_volume(catalog_name, SCHEMA_BRONZE, VOLUME_NAME)
        _set_progress(user_id, 6, f'Volume ready: {VOLUME_NAME}')
    except Exception as e:
        _set_progress(user_id, 6, '', error=f'Volume creation failed: {e}')
        raise RuntimeError(f'Step 6 failed: {e}')

    # ------------------------------------------------------------------
    # Step 7 — Validate volume path
    # ------------------------------------------------------------------
    _set_progress(user_id, 7, f'Validating volume path: {volume_path}...')
    if not dbx.validate_volume_path(volume_path):
        # Volume may take a moment to be ready — retry once
        import time; time.sleep(3)
        if not dbx.validate_volume_path(volume_path):
            err = f'Volume path {volume_path} not accessible via Files API'
            _set_progress(user_id, 7, '', error=err)
            raise RuntimeError(f'Step 7 failed: {err}')
    _set_progress(user_id, 7, f'Volume path confirmed: {volume_path}')

    # ------------------------------------------------------------------
    # Step 8 — Create empty _meta_* registry tables.
    #
    # Creates <catalog>.bronze._meta_bronze_pk_registry and
    # _meta_bronze_table_status. PK rows are NOT seeded here — they
    # arrive in autodesk_data_extract.zip on each sync run, and
    # sync_service MERGEs them into the registry post-upload.
    # ------------------------------------------------------------------
    _set_progress(user_id, 8, 'Creating PK registry / status tables...')
    try:
        _create_meta_tables(dbx, warehouse_id, catalog_name)
        _set_progress(
            user_id, 8,
            f'PK registry ready — empty until first sync seeds it from '
            f'autodesk_data_extract.zip',
        )
    except Exception as e:
        _set_progress(user_id, 8, '', error=f'_meta_* table creation failed: {e}')
        raise RuntimeError(f'Step 8 failed: {e}')

    # ------------------------------------------------------------------
    # Step 8.5 — Upload pk_config_template.json to Volume
    #
    # Uploads a default PK configuration template that the operator can
    # customize. The pipeline will use this to seed the PK registry instead
    # of deriving PKs from ordinal_position.
    # ------------------------------------------------------------------
    _set_progress(user_id, 8, 'Uploading pk_config_template.json to Volume...')
    try:
        # pk_config_file = Path(__file__).parent.parent / 'config' / 'pk_config_template.json'
       
        pk_config_file = Path(__file__).parent.parent.parent / 'config' / 'pk_config_template.json'
       
        if not pk_config_file.exists():
            raise FileNotFoundError(f'pk_config_template.json not found: {pk_config_file}')
        
        pk_config_content = pk_config_file.read_text()
        pk_config_volume_path = f'{volume_path}/pk_config.json'
        
        dbx.put_file(pk_config_volume_path, pk_config_content.encode())
        logger.info('Uploaded pk_config.json to %s', pk_config_volume_path)
        _set_progress(user_id, 8, f'pk_config.json uploaded to Volume')
    except Exception as e:
        _set_progress(user_id, 8, '', error=f'pk_config upload failed: {e}')
        raise RuntimeError(f'Step 8.5 failed: {e}')
            
    # ------------------------------------------------------------------
    # Step 9 — Upload notebooks
    # ------------------------------------------------------------------
    _set_progress(user_id, 9, 'Uploading notebooks to Databricks workspace...')
    try:
        user_email      = dbx.get_current_user_email()
        notebook_folder = f'/Users/{user_email}/acc/v1'
        dbx.mkdirs(notebook_folder)

        # acc_snapshot_pipeline   — Pipeline A (snapshot-only groups)
        # acc_delta_cdc_pipeline  — Pipeline B (CDC groups, ONCE + ongoing)
        # acc_pipeline_test       — standalone test harness (no bootstrap)
        # auto_cdc_*              — legacy rollback notebooks
        # bulk_downloader         — DC signed-URL download Job task
        _notebook_names = (
            'acc_snapshot_pipeline',
            'acc_delta_cdc_pipeline',
            'acc_pipeline_test',
            'auto_cdc_pipeline',
            'auto_cdc_cdc_pipeline',
            'bulk_downloader',
            'seed_registry',
        )
        for nb_name in _notebook_names:
            nb_file = NOTEBOOKS_DIR / f'{nb_name}.py'
            if not nb_file.exists():
                raise FileNotFoundError(f'Notebook file not found: {nb_file}')
            content_b64 = base64.b64encode(nb_file.read_bytes()).decode()
            dbx.import_notebook(f'{notebook_folder}/{nb_name}', content_b64)
            logger.info('Uploaded notebook: %s/%s', notebook_folder, nb_name)

        shared_dir = NOTEBOOKS_DIR / 'shared'
        if shared_dir.is_dir():
            dbx.mkdirs(f'{notebook_folder}/shared')
            for shared_file in shared_dir.glob('*.py'):
                content_b64 = base64.b64encode(shared_file.read_bytes()).decode()
                shared_name = shared_file.stem
                dbx.import_notebook(
                    f'{notebook_folder}/shared/{shared_name}', content_b64,
                )
                logger.info('Uploaded shared notebook: %s/shared/%s', notebook_folder, shared_name)

        _set_progress(user_id, 9, f'Notebooks uploaded to {notebook_folder}')
    except Exception as e:
        _set_progress(user_id, 9, '', error=f'Notebook upload failed: {e}')
        raise RuntimeError(f'Step 9 failed: {e}')

    _enable_nb_download = os.getenv('ENABLE_NOTEBOOK_DOWNLOAD', 'false').lower() == 'true'
    if not _enable_nb_download:
        _set_progress(
            user_id, 9,
            'Notebook download disabled — sync uses Flask volume upload path',
        )

    # ------------------------------------------------------------------
    # Step 10 — Create Bronze AUTO CDC FROM SNAPSHOT pipeline.
    #
    # Lakeflow Spark Declarative Pipeline. Requires Pro / Advanced /
    # Serverless edition; the API returns 400 on Standard, which we
    # SCD Type 1 is the connector's terminal layer (latest row per PK).
    # ------------------------------------------------------------------
    _set_progress(user_id, 10, 'Creating Databricks Snapshot Pipeline...')
    try:
        snapshot_pipeline_config = {
            'name':    names['snapshot_pipeline'],
            # Serverless pipelines require ADVANCED edition (the Pipelines API
            # rejects serverless=true on PRO with INVALID_PARAMETER_VALUE).
            # ADVANCED also adds expectations/quality, which we don't yet use
            # but pay for as part of bundling with serverless.
            'edition': 'ADVANCED',
            # Unity-Catalog-published pipeline. ``catalog`` + ``target`` make
            # tables land in <catalog>.bronze.<table_name>.
            'catalog':   catalog_name,
            'target':    SCHEMA_BRONZE,
            'continuous': False,
            'serverless': True,
            'libraries': [{
                'notebook': {'path': f'{notebook_folder}/acc_snapshot_pipeline'},
            }],
            # Pipeline configuration — surfaced as ``spark.conf.get('acc.catalog')``
            # inside the notebook so a single notebook drives any tenant's catalog.
            'configuration': {
                'acc.catalog':        catalog_name,
                'acc.volume_layout':  'legacy',
                'acc.test_mode':      'false',
            },
        }

        snapshot_pipeline_id = dbx.get_or_create_pipeline(
            names['snapshot_pipeline'], snapshot_pipeline_config,
        )

        _set_progress(
            user_id, 10,
            f'Pipeline ready — {names["snapshot_pipeline"]}: {snapshot_pipeline_id}',
        )
    except Exception as e:
        _set_progress(user_id, 10, '', error=f'Pipeline creation failed: {e}')
        raise RuntimeError(f'Step 10 failed: {e}')

    # ------------------------------------------------------------------
    # Step 10b — Bronze AUTO CDC pipeline (CDC-beta deltas).
    # ------------------------------------------------------------------
    _set_progress(user_id, 10, 'Creating Databricks CDC pipeline...')
    try:
        cdc_pipeline_config = {
            'name':       names['cdc_pipeline'],
            'edition':    'ADVANCED',
            'catalog':    catalog_name,
            'target':     SCHEMA_BRONZE,
            'continuous': False,
            'serverless': True,
            'libraries': [{
                'notebook': {'path': f'{notebook_folder}/acc_delta_cdc_pipeline'},
            }],
            'configuration': {
                'acc.catalog':        catalog_name,
                'acc.volume_layout':  'legacy',
                'acc.test_mode':      'false',
                **CDC_PIPELINE_TUNING,
            },
        }
        cdc_pipeline_id = dbx.get_or_create_pipeline(
            names['cdc_pipeline'], cdc_pipeline_config,
        )
        _set_progress(
            user_id, 10,
            f'Pipelines ready — snapshot: {snapshot_pipeline_id}, CDC: {cdc_pipeline_id}',
        )
    except Exception as e:
        _set_progress(user_id, 10, '', error=f'CDC pipeline creation failed: {e}')
        raise RuntimeError(f'Step 10b failed: {e}')

    # ------------------------------------------------------------------
    # Step 10c — Sync workflows (download → pipeline) when notebook path on.
    # ------------------------------------------------------------------
    snapshot_workflow_id = None
    cdc_workflow_id = None
    if _enable_nb_download:
        _set_progress(user_id, 10, 'Creating snapshot + CDC sync workflows...')
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
            _set_progress(
                user_id, 10,
                f'Workflows ready — snapshot: {snapshot_workflow_id}, CDC: {cdc_workflow_id}',
            )
        except Exception as e:
            _set_progress(user_id, 10, '', error=f'Sync workflow creation failed: {e}')
            raise RuntimeError(f'Step 10c failed: {e}')

    # ------------------------------------------------------------------
    # Step 11 — Persist bootstrap state
    # ------------------------------------------------------------------
    _set_progress(user_id, 11, 'Saving bootstrap configuration...')
    db.save_bootstrap_state(
        user_id=user_id,
        snapshot_pipeline_id=snapshot_pipeline_id,
        cdc_pipeline_id=cdc_pipeline_id,
        notebook_folder=notebook_folder,
        volume_path=volume_path,
        compute_type=compute_type,
        warehouse_id=warehouse_id,
        catalog_name=catalog_name,
        snapshot_workflow_id=snapshot_workflow_id,
        cdc_workflow_id=cdc_workflow_id,
        catalog_claim_id=claim['claim_id'],
    )
    db.update_claim_artifacts(
        claim['claim_id'],
        snapshot_pipeline_id=snapshot_pipeline_id,
        cdc_pipeline_id=cdc_pipeline_id,
        snapshot_pipeline_name=names['snapshot_pipeline'],
        cdc_pipeline_name=names['cdc_pipeline'],
        snapshot_workflow_id=snapshot_workflow_id,
        cdc_workflow_id=cdc_workflow_id,
    )

    acc_cfg = db.get_acc_config(user_id)
    if acc_cfg and acc_cfg.get('project_id'):
        bare_pid = acc_cfg['project_id']
        if bare_pid.startswith('b.'):
            bare_pid = bare_pid[2:]
        try:
            from backend.services.sync.pipeline_config import sync_project_pipeline_defaults
            sync_project_pipeline_defaults(
                dbx,
                {
                    'catalog_name':       catalog_name,
                    'snapshot_pipeline_id': snapshot_pipeline_id,
                    'cdc_pipeline_id':    cdc_pipeline_id,
                },
                bare_pid,
            )
        except Exception as exc:
            logger.warning('Bootstrap: pipeline default config sync failed: %s', exc)

    _set_progress(user_id, 11, 'Bootstrap complete!', done=True)

    return {
        'snapshot_pipeline_id': snapshot_pipeline_id,
        'cdc_pipeline_id':        cdc_pipeline_id,
        'snapshot_workflow_id':   snapshot_workflow_id,
        'cdc_workflow_id':        cdc_workflow_id,
        'notebook_folder':        notebook_folder,
        'volume_path':        volume_path,
        'compute_type':       compute_type,
        'warehouse_id':       warehouse_id,
        'catalog_name':       catalog_name,
        'catalog_claim_id':   claim['claim_id'],
        'pipeline_names':     names,
    }
