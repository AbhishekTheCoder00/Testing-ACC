# Databricks notebook source
# ACC Pipeline A — Snapshot-only service groups (10 groups).
#
# Uses create_auto_cdc_from_snapshot_flow() with SCD Type 1 (latest row per PK).
# Skips domains that have a CDC mirror (admin, issues, cost, …) — those are
# owned exclusively by acc_delta_cdc_pipeline.py (Pipeline B).
#
# Volume paths (acc.volume_layout):
#   legacy — acc.dc_snapshot_path or .../data_connector/<project>/<run>/*.csv
#   v2     — .../raw/snapshots/{service_group}/*.csv

# MAGIC %run ./shared/service_groups_config

# MAGIC %run ./shared/acc_pipeline_common

import dlt
from pyspark.sql import functions as F


def _snapshot_version_for_path(path: str) -> int:
    """Map a snapshot file path to a monotonic integer version."""
    if not path:
        return 0
    tail = path.rstrip('/').split('/')[-1]
    # ISO run folder e.g. 2026-05-27T12-00-00
    if 'T' in tail and len(tail) >= 19:
        return int(''.join(c for c in tail if c.isdigit())[:14] or '0')
    return hash(path) & 0x7FFFFFFF


def _register_snapshot_table(
    csv_name: str,
    csv_path: str,
    registry: dict,
    known_schemas: set,
    evolved_schemas: dict,
    schema_changed: bool,
) -> None:
    """Register one snapshot-only table with versioned AUTO CDC FROM SNAPSHOT."""
    table_name = _table_name_from_csv(csv_name)
    gated = gate_table_common(
        csv_name, registry, known_schemas, csv_path, evolved_schemas, schema_changed,
    )
    if gated is None:
        return

    sch = gated['sch']
    tbl = gated['tbl']
    pk_columns = gated['pk_columns']
    header = gated['header']
    column_diff = gated['column_diff']
    evolved_struct = gated['evolved_struct']

    if not is_snapshot_only_schema(sch):
        _emit('INFO', f'{sch}.{tbl}: skipped — owned by Pipeline B (CDC mirror exists)')
        _record_status(
            sch, tbl, STATUS_SKIPPED_NO_CSV, csv_header=header,
            error_msg='CDC-capable domain; use acc_delta_cdc_pipeline',
        )
        _run_counts[STATUS_SKIPPED_NO_CSV] += 1
        return

    if not snapshot_service_group_allowed(sch):
        _emit('INFO', f'{sch}.{tbl}: skipped — outside acc.snapshot_service_groups filter')
        _record_status(
            sch, tbl, STATUS_SKIPPED_NO_CSV, csv_header=header,
            error_msg='filtered by acc.snapshot_service_groups',
        )
        _run_counts[STATUS_SKIPPED_NO_CSV] += 1
        return

    snapshot_path = csv_path
    snapshot_version = _snapshot_version_for_path(snapshot_path)

    def _make_next_snapshot(_path=snapshot_path, _version=snapshot_version,
                            _evolved=evolved_struct, _sch=sch, _tbl=tbl):
        def next_snapshot(latest_version):
            if latest_version is not None and latest_version >= _version:
                return None
            if not _fs_exists(_path):
                return None
            df = read_csv_df(_path, _evolved, filter_deleted_at=True)
            _emit('INFO', f'{_sch}.{_tbl}: snapshot v{_version} from {_path}')
            return (df, _version)
        return next_snapshot

    dlt.create_streaming_table(
        name=table_name,
        schema=_schema_for_bronze_table(evolved_struct),
        comment=f'ACC snapshot-only -> SCD1 Bronze ({csv_name}). PK: {pk_columns}',
    )
    dlt.create_auto_cdc_from_snapshot_flow(
        target=table_name,
        source=_make_next_snapshot(),
        keys=pk_columns,
        stored_as_scd_type=1,
    )

    _record_status(sch, tbl, STATUS_OK, csv_header=header, column_diff=column_diff)
    _run_counts[STATUS_OK] += 1
    _emit('OK', f'{sch}.{tbl}: snapshot AUTO CDC registered, keys={pk_columns}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
(
    _registry_flat,
    _known_schemas,
    _typed_schemas,
    _schema_changed,
    _evolved_schemas,
    _snapshot_base,
) = prepare_shared_bootstrap()

_processed_keys: set[tuple[str, str]] = set()

if VOLUME_LAYOUT == 'v2' and not DC_SNAPSHOT_PATH:
    for csv_name, csv_path in discover_all_csv_basenames(RAW_SNAPSHOTS_BASE):
        table_name = _table_name_from_csv(csv_name)
        sch, tbl = _resolve_schema_table(csv_name, table_name, _registry_flat, _known_schemas)
        if not is_snapshot_only_schema(sch):
            continue
        if not snapshot_service_group_allowed(sch):
            continue
        _processed_keys.add((sch, tbl))
        _register_snapshot_table(
            csv_name, csv_path, _registry_flat, _known_schemas,
            _evolved_schemas, _schema_changed,
        )
else:
    if not _snapshot_base:
        _emit('WARN', 'No snapshot base resolved — no tables registered')
    for _csv in _discover_csv_basenames(_snapshot_base):
        _tbl = _table_name_from_csv(_csv)
        _flat = _tbl.lower()
        if _flat in _registry_flat:
            sch = _registry_flat[_flat]['schema']
            tbl = _registry_flat[_flat]['table']
        else:
            sch, tbl = _split_csv_stem(_tbl, _known_schemas)
        if not snapshot_service_group_allowed(sch):
            continue
        if is_snapshot_only_schema(sch):
            _processed_keys.add((sch, tbl))
        _register_snapshot_table(
            _csv,
            f'{_snapshot_base}/{_csv}',
            _registry_flat,
            _known_schemas,
            _evolved_schemas,
            _schema_changed,
        )

_sweep_heartbeat(_processed_keys)
emit_summary()
