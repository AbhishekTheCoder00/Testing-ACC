# Databricks notebook source
# ACC Pipeline — lightweight test harness (no bootstrap required).
#
# Run as a standalone Lakeflow Pipeline with:
#   acc.test_mode = true
#   acc.catalog = <your_catalog>
#   acc.fixture_base = /Volumes/<catalog>/bronze/acc_bronze_volume/test_fixtures
#
# Prepare fixtures (one-time, outside this notebook):
#   1. Create fixture_base/snapshot/assets_assets.csv  (snapshot-only table)
#   2. Create fixture_base/snapshot/cdcissues_issues.csv (CDC table with adsk_updated_at)
#   3. Optionally copy pk_config.json + schema.json to volume root OR rely on inline PKs
#
# Widgets below override spark.conf for ad-hoc notebook runs.

dbutils.widgets.text('catalog', 'final_poc')
dbutils.widgets.dropdown('test_group', 'both', ['snapshot', 'delta', 'both'])
dbutils.widgets.text('fixture_base', '')

_catalog = dbutils.widgets.get('catalog').strip() or 'final_poc'
_test_group = dbutils.widgets.get('test_group').strip() or 'both'
_fixture = dbutils.widgets.get('fixture_base').strip()
if not _fixture:
    _fixture = f'/Volumes/{_catalog}/bronze/acc_bronze_volume/test_fixtures'

spark.conf.set('acc.catalog', _catalog)
spark.conf.set('acc.test_mode', 'true')
spark.conf.set('acc.volume_layout', 'v2')
spark.conf.set('acc.fixture_base', _fixture)

# MAGIC %run ./shared/service_groups_config

# MAGIC %run ./shared/acc_pipeline_common

import dlt
from pyspark.sql import functions as F

FIXTURE_BASE = spark.conf.get('acc.fixture_base', _fixture).strip()
SNAPSHOT_FIXTURE_DIR = f'{FIXTURE_BASE}/snapshot'
DELTA_FIXTURE_DIR = f'{FIXTURE_BASE}/delta'


def _ensure_test_fixtures() -> None:
    """Create minimal CSV fixtures if missing (idempotent for dev workspaces)."""
    try:
        dbutils.fs.mkdirs(SNAPSHOT_FIXTURE_DIR)
        dbutils.fs.mkdirs(DELTA_FIXTURE_DIR)
    except Exception:
        pass

    assets_csv = f'{SNAPSHOT_FIXTURE_DIR}/assets_assets.csv'
    cdc_csv = f'{SNAPSHOT_FIXTURE_DIR}/cdcissues_issues.csv'
    delta_csv = f'{DELTA_FIXTURE_DIR}/cdcissues_issues.csv'

    if not _fs_exists(assets_csv):
        dbutils.fs.put(
            assets_csv,
            'id,name,status\n1,Asset A,active\n2,Asset B,active\n',
            overwrite=True,
        )
        _emit('INFO', f'Created fixture {assets_csv}')

    if not _fs_exists(cdc_csv):
        dbutils.fs.put(
            cdc_csv,
            'id,title,adsk_updated_at,deleted_at\n'
            '1,Issue A,2024-01-15T10:00:00,\n'
            '2,Issue B,2024-01-16T11:00:00,\n',
            overwrite=True,
        )
        _emit('INFO', f'Created fixture {cdc_csv}')

    if not _fs_exists(delta_csv):
        dbutils.fs.put(
            delta_csv,
            'id,title,adsk_updated_at,deleted_at\n'
            '1,Issue A Updated,2024-01-17T12:00:00,\n',
            overwrite=True,
        )
        _emit('INFO', f'Created fixture {delta_csv}')


def _register_test_snapshot_table(csv_name: str, csv_path: str) -> None:
    table_name = _table_name_from_csv(csv_name)
    sch, tbl = _split_csv_stem(table_name, {'assets'})
    if sch != 'assets':
        return
    pk_columns = ['id']
    header = _read_csv_header(csv_path)

    @dlt.view(name=f'v_test_{table_name}')
    def _src(_path=csv_path):
        return read_csv_df(_path, None, filter_deleted_at=True)

    def _next_snapshot(_path=csv_path):
        def next_snapshot(latest_version):
            if latest_version is not None and latest_version >= 1:
                return None
            return (read_csv_df(_path, None, filter_deleted_at=True), 1)
        return next_snapshot

    dlt.create_streaming_table(name=table_name, comment='Test snapshot-only table')
    dlt.create_auto_cdc_from_snapshot_flow(
        target=table_name,
        source=_next_snapshot(),
        keys=pk_columns,
        stored_as_scd_type=1,
    )
    _ensure_run_counts()
    _run_counts[STATUS_OK] += 1
    _emit('OK', f'TEST snapshot table {sch}.{tbl} registered from {csv_path}')


def _register_test_cdc_table(csv_name: str, snapshot_path: str, delta_path: str) -> None:
    table_name = _table_name_from_csv(csv_name)
    sch, tbl = _split_csv_stem(table_name, {'cdcissues'})
    if sch != 'cdcissues':
        return
    pk_columns = ['id']
    sequence_col = 'adsk_updated_at'
    flow_kw = dict(
        target=table_name,
        keys=pk_columns,
        sequence_by=F.col(sequence_col),
        apply_as_deletes=F.expr('deleted_at IS NOT NULL'),
        stored_as_scd_type=1,
    )

    dlt.create_streaming_table(name=table_name, comment='Test CDC table')

    once_view = f'v_test_once_{table_name}'

    @dlt.view(name=once_view)
    def _once(_path=snapshot_path):
        return read_csv_df(_path, None, filter_deleted_at=False)

    dlt.create_auto_cdc_flow(source=once_view, name=f'{table_name}_once', once=True, **flow_kw)

    delta_view = f'v_test_delta_{table_name}'

    @dlt.view(name=delta_view)
    def _delta(_path=delta_path):
        return read_csv_df(_path, None, filter_deleted_at=False)

    dlt.create_auto_cdc_flow(source=delta_view, name=f'{table_name}_ongoing', **flow_kw)
    _ensure_run_counts()
    _run_counts[STATUS_OK] += 1
    _emit('OK', f'TEST CDC table {sch}.{tbl} registered (once+ongoing)')


# ---------------------------------------------------------------------------
# Main — minimal tables, no bootstrap meta MERGE
# ---------------------------------------------------------------------------
_ensure_test_fixtures()

if _test_group in ('snapshot', 'both'):
    _register_test_snapshot_table(
        'assets_assets.csv',
        f'{SNAPSHOT_FIXTURE_DIR}/assets_assets.csv',
    )

if _test_group in ('delta', 'both'):
    _register_test_cdc_table(
        'cdcissues_issues.csv',
        f'{SNAPSHOT_FIXTURE_DIR}/cdcissues_issues.csv',
        f'{DELTA_FIXTURE_DIR}/cdcissues_issues.csv',
    )

emit_summary()
