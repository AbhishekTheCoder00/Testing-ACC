
import logging
import os

DC_WATERMARK_KEY = 'data_connector'
DC_CDC_WATERMARK_KEY = 'data_connector_cdc'
MANUAL_FULL_WATERMARK_KEY = 'data_connector_manual_full'
MANUAL_FULL_MIN_INTERVAL_SEC = 24 * 3600
_LEGACY_VOLUME_BASE = '/Volumes/final_poc/bronze/acc_bronze_volume'

# Name of the consolidated extract zip Autodesk's Data Connector publishes
# alongside the per-table CSVs. Inside the zip there's a `schemas/`
# directory which can hold either a single consolidated `schema.json`
# (3-level: {schema: {table: {column: {...}}}}) or one file per ACC
# domain such as `admin.json`, `issues.json` (2-level: {table: {column:
# {...}}}, schema name = filename stem). _load_schema_doc_from_zip
# handles both layouts and returns one normalized 3-level dict.
_DC_EXTRACT_ZIP_NAME = 'autodesk_data_extract.zip'
_DC_SCHEMAS_DIR = 'schemas/'
_DC_CONSOLIDATED_NAME = 'schema.json'
_DC_EXPORT_SCHEMAS_PREFIX = 'docs/schemas/'
_DC_METADATA_FILE_NAME = 'metadata.csv'

# _meta_* table names — kept in sync with bootstrap.py and
# notebooks/auto_cdc_pipeline.py.
_META_BRONZE_SCHEMA = 'bronze'
_META_REGISTRY_TABLE = '_meta_bronze_pk_registry'

# MERGE batch size for registry seeding. Spark SQL handles large VALUES
# lists fine, but smaller batches keep individual statement parse times
# bounded and produce readable progress logs.
_REGISTRY_SEED_BATCH = 50

# Feature flag — when set, Phase 2 writes a manifest and a Databricks sync
# workflow (bulk_downloader → Bronze pipeline) runs in the workspace.
# PK registry seeding is handled in the pipeline notebook from schema.json.
_ENABLE_NOTEBOOK_DOWNLOAD = os.environ.get('ENABLE_NOTEBOOK_DOWNLOAD', 'false').lower() == 'true'

# Idle timeout while polling a Databricks sync workflow (bulk_download → pipelines).
# Resets on any task state change; first full sync often needs 60–120+ minutes.
_DEFAULT_WORKFLOW_MAX_WAIT_MIN = 120


def workflow_max_wait_min() -> int:
    return int(os.getenv(
        'SYNC_WORKFLOW_MAX_WAIT_MIN',
        str(_DEFAULT_WORKFLOW_MAX_WAIT_MIN),
    ))


# Idle timeout while polling Lakeflow pipeline updates (Flask path, not notebook workflow).
# First snapshot baseline (~260 tables, SCD2) often exceeds 30 min on serverless.
_DEFAULT_SNAPSHOT_PIPELINE_MAX_WAIT_MIN = 90
_DEFAULT_CDC_PIPELINE_MAX_WAIT_MIN = 90


def snapshot_pipeline_max_wait_min() -> int:
    return int(os.getenv(
        'SNAPSHOT_PIPELINE_MAX_WAIT_MIN',
        str(_DEFAULT_SNAPSHOT_PIPELINE_MAX_WAIT_MIN),
    ))


def cdc_pipeline_max_wait_min() -> int:
    return int(os.getenv(
        'CDC_PIPELINE_MAX_WAIT_MIN',
        str(_DEFAULT_CDC_PIPELINE_MAX_WAIT_MIN),
    ))


# Dev/pilot only — set SYNC_TEST_SUBSET=true to export assets + cdcsheets for faster runs.
# Production default (unset env): full export.
_SYNC_TEST_SUBSET = os.getenv('SYNC_TEST_SUBSET', 'false').lower() == 'true'
TEST_SNAPSHOT_EXPORT_GROUPS = ['assets']
TEST_CDC_EXPORT_GROUPS = ['cdcadmin']


def dc_snapshot_service_groups() -> list | None:
    """Return narrowed export groups for testing, or None for full default."""
    if _SYNC_TEST_SUBSET:
        return list(TEST_SNAPSHOT_EXPORT_GROUPS)
    return None


def dc_cdc_service_groups() -> list | None:
    """Return narrowed CDC export groups for testing, or None for full default."""
    if _SYNC_TEST_SUBSET:
        return list(TEST_CDC_EXPORT_GROUPS)
    return None


def is_sync_test_subset_enabled() -> bool:
    return _SYNC_TEST_SUBSET


def pipeline_test_subset_conf() -> dict[str, str]:
    """Spark conf keys so Lakeflow pipelines honor the same subset as DC export."""
    if not _SYNC_TEST_SUBSET:
        return {
            'acc.sync_test_subset':        'false',
            'acc.snapshot_service_groups': '',
            'acc.cdc_service_groups':      '',
        }
    return {
        'acc.sync_test_subset':        'true',
        'acc.snapshot_service_groups': ','.join(TEST_SNAPSHOT_EXPORT_GROUPS),
        'acc.cdc_service_groups':      ','.join(TEST_CDC_EXPORT_GROUPS),
    }
