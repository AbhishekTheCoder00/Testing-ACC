# Databricks notebook source
# ACC Pipeline B — Delta CDC service groups (10 official cdc* groups).
#
# Legacy layout (acc.dc_cdc_path = latest timestamp folder each sync):
#   One create_auto_cdc_flow per table — readStream on project CDC root
#   (.../data_connector_cdc/{project_id}/) with recursiveFileLookup + pathGlobFilter.
#   Sync CDC uses full_refresh=false (incremental); Full Refresh CDC uses true.
#
# v2 layout (raw/deltas/{group}/*/{csv}):
#   ONCE bootstrap + ongoing Auto Loader on stable dated subfolders.

# MAGIC %run ./shared/service_groups_config

# MAGIC %run ./shared/acc_pipeline_common

import dlt
from pyspark.sql import functions as F


def _sequence_column(header: set) -> str | None:
    if 'adsk_updated_at' in header:
        return 'adsk_updated_at'
    if 'updated_at' in header:
        return 'updated_at'
    return None


def _resolve_csv_stream_source(
    service_group: str, csv_name: str, cdc_base: str,
) -> tuple[str, str] | None:
    """Return (project_cdc_root, pathGlobFilter) for readStream on one CSV file.

    spark.readStream.format('csv').load() requires a directory — not a file path.
    Legacy CDC CSVs live under timestamp folders; the stream root is the stable
    project parent so checkpoints do not reset when acc.dc_cdc_path changes.
    """
    if cdc_base:
        project_root = resolve_project_cdc_root(cdc_base)
        if project_root and cdc_csv_exists_in_project(project_root, csv_name):
            return project_root.rstrip('/'), csv_name
    if VOLUME_LAYOUT == 'v2':
        stream_dir = f'{RAW_SNAPSHOTS_BASE}/{service_group}'.rstrip('/')
        if _fs_exists(f'{stream_dir}/{csv_name}'):
            return stream_dir, csv_name
    return None


def _read_csv_stream(stream_dir: str, path_glob_filter: str, evolved_struct) -> 'DataFrame':
    """CSV via Structured Streaming — project root + recursive lookup (SDP-safe)."""
    reader = (
        spark.readStream.format('csv')
        .option('header', True)
        .option('multiLine', True)
        .option('mode', 'PERMISSIVE')
        .option('nullValue', '')
        .option('emptyValue', '')
        .option('recursiveFileLookup', 'true')
        .option('pathGlobFilter', path_glob_filter)
    )
    if evolved_struct is not None:
        reader = reader.schema(evolved_struct)
    return reader.load(stream_dir.rstrip('/'))


def _register_cdc_table(
    csv_name: str,
    csv_path: str,
    registry: dict,
    known_schemas: set,
    evolved_schemas: dict,
    schema_changed: bool,
    cdc_base: str,
) -> None:
    """Register AUTO CDC flows for one CDC table."""
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
    header_set = set(header)

    if not is_delta_cdc_schema(sch):
        _emit('INFO', f'{sch}.{tbl}: skipped — not a CDC service group (Pipeline A owner)')
        return

    if not cdc_service_group_allowed(sch):
        _emit('INFO', f'{sch}.{tbl}: skipped — outside acc.cdc_service_groups filter')
        return

    pk_source = gated.get('pk_source', 'unknown')
    try:
        audit_status, audit_msg, _ = audit_table_pk(
            csv_path, sch, tbl, table_name, pk_columns, pk_source, header=header,
            prefer_run_path=cdc_base or DC_CDC_PATH,
        )
    except FileNotFoundError:
        _emit('WARN', f'{sch}.{tbl}: skipped — CSV not found for PK audit')
        return
    except Exception as exc:
        _emit('ERROR', f'{sch}.{tbl}: PK audit failed ({exc})')
        _record_status(sch, tbl, STATUS_CSV_UNREADABLE, csv_header=header, error_msg=str(exc))
        _run_counts[STATUS_CSV_UNREADABLE] += 1
        return

    if audit_status in PK_AUDIT_BLOCK_STATUSES:
        _emit('ERROR', f'{sch}.{tbl}: PK audit blocked CDC — {audit_status}')
        _record_status(sch, tbl, audit_status, csv_header=header, error_msg=audit_msg)
        _run_counts[audit_status] = _run_counts.get(audit_status, 0) + 1
        return

    if audit_status == STATUS_PK_NOT_UNIQUE:
        _emit(
            'WARN',
            f'{sch}.{tbl}: pk_not_unique in audit file (CDC may still merge by '
            f'sequence column) — registering table',
        )

    sequence_col = _sequence_column(header_set)
    if not sequence_col:
        _emit('ERROR', f'{sch}.{tbl}: skipped — no adsk_updated_at or updated_at in header')
        _record_status(
            sch, tbl, STATUS_CSV_UNREADABLE, csv_header=header,
            error_msg='missing sequence column for AUTO CDC',
        )
        _run_counts[STATUS_CSV_UNREADABLE] += 1
        return

    stream_source = _resolve_csv_stream_source(sch, csv_name, cdc_base)
    delta_glob = resolve_delta_glob(sch, csv_name)
    use_v2_autoloader = VOLUME_LAYOUT == 'v2' and not DC_CDC_PATH
    use_legacy_run_dir = bool(cdc_base) and not use_v2_autoloader

    pk_expect_expr = ' AND '.join(f'`{c}` IS NOT NULL' for c in pk_columns)
    flow_kw_base = dict(
        target=table_name,
        keys=pk_columns,
        sequence_by=F.col(sequence_col),
        stored_as_scd_type=1,
    )
    if 'deleted_at' in header_set:
        flow_kw_base['apply_as_deletes'] = F.expr('deleted_at IS NOT NULL')

    dlt.create_streaming_table(
        name=table_name,
        schema=_schema_for_bronze_table(evolved_struct),
        comment=f'ACC CDC -> SCD1 Bronze ({csv_name}). PK: {pk_columns}',
    )

    if use_legacy_run_dir:
        if not stream_source:
            project_root = resolve_project_cdc_root(cdc_base)
            _emit('WARN', f'{sch}.{tbl}: no CSV under {project_root or cdc_base} — skipped')
            return
        stream_dir, stream_filter = stream_source
        view_name = f'v_cdc_{table_name}'

        @dlt.view(name=view_name)
        @dlt.expect('pk_not_null', pk_expect_expr)
        def _legacy_cdc_src(
            _dir=stream_dir, _filter=stream_filter,
            _evolved=evolved_struct, _sch=sch, _tbl=tbl,
        ):
            _emit(
                'INFO',
                f'{_sch}.{_tbl}: CDC stream root={_dir} filter={_filter} recursive=true',
            )
            return _read_csv_stream(_dir, _filter, _evolved)

        dlt.create_auto_cdc_flow(
            source=view_name,
            name=f'{table_name}_cdc',
            **flow_kw_base,
        )
    else:
        if stream_source:
            once_dir, once_filter = stream_source
            once_view = f'v_once_{table_name}'

            @dlt.view(name=once_view)
            @dlt.expect('pk_not_null', pk_expect_expr)
            def _once_src(
                _dir=once_dir, _filter=once_filter,
                _evolved=evolved_struct, _sch=sch, _tbl=tbl,
            ):
                _emit('INFO', f'{_sch}.{_tbl}: ONCE stream dir={_dir} filter={_filter}')
                return _read_csv_stream(_dir, _filter, _evolved)

            dlt.create_auto_cdc_flow(
                source=once_view,
                name=f'{table_name}_once',
                once=True,
                **flow_kw_base,
            )
        else:
            _emit('WARN', f'{sch}.{tbl}: no snapshot path for ONCE flow — ongoing only')

        ongoing_view = f'v_delta_{table_name}'

        @dlt.view(name=ongoing_view)
        @dlt.expect('pk_not_null', pk_expect_expr)
        def _delta_src(_glob=delta_glob, _evolved=evolved_struct, _sch=sch, _tbl=tbl):
            _emit('INFO', f'{_sch}.{_tbl}: Auto Loader on {_glob}')
            reader = (
                spark.readStream.format('cloudFiles')
                .option('cloudFiles.format', 'csv')
                .option('header', 'true')
                .option('cloudFiles.inferColumnTypes', 'true')
                .option('cloudFiles.schemaEvolutionMode', 'addNewColumns')
                .option('multiLine', 'true')
                .option('mode', 'PERMISSIVE')
                .option('nullValue', '')
                .option('emptyValue', '')
            )
            if _evolved is not None:
                reader = reader.schema(_evolved)
            return reader.load(_glob)

        dlt.create_auto_cdc_flow(
            source=ongoing_view,
            name=f'{table_name}_ongoing',
            **flow_kw_base,
        )

    _record_status(
        sch, tbl, audit_status, csv_header=header, column_diff=column_diff,
        error_msg=audit_msg if audit_status != STATUS_OK else None,
    )
    if audit_status == STATUS_OK:
        _run_counts[STATUS_OK] += 1
    else:
        _run_counts[audit_status] = _run_counts.get(audit_status, 0) + 1
        _run_counts['ingested_with_audit_warn'] = _run_counts.get('ingested_with_audit_warn', 0) + 1
    _emit(
        'OK',
        f'{sch}.{tbl}: CDC flows registered, keys={pk_columns}, seq={sequence_col}'
        + (f' (audit={audit_status})' if audit_status != STATUS_OK else ''),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
(
    _registry_flat,
    _known_schemas,
    _typed_schemas,
    _schema_changed,
    _evolved_schemas,
    _cdc_base,
) = prepare_shared_bootstrap(resolve_base_fn=_resolve_cdc_base)

_cdc_typed = load_cdc_typed_schemas_from_volume()
if _cdc_typed:
    seed_cdc_pk_registry_from_schemas(_cdc_typed)
    _registry_flat, _known_schemas = _load_registry()
    for _key, _struct in _cdc_typed.items():
        if _key not in _evolved_schemas:
            _evolved_schemas[_key] = _struct
        if _schema_changed:
            sch, tbl = _split_csv_stem(_key, _known_schemas)
            if not cdc_service_group_allowed(sch):
                continue
            evolved, _ = _build_evolved_schema(_struct, _key, sch, tbl)
            _evolved_schemas[_key] = evolved

_processed_keys: set[tuple[str, str]] = set()

if VOLUME_LAYOUT == 'v2' and not DC_CDC_PATH:
    for csv_name, csv_path in discover_all_csv_basenames(RAW_SNAPSHOTS_BASE):
        table_name = _table_name_from_csv(csv_name)
        grp = cdc_group_for_csv_stem(table_name)
        if not grp or not cdc_service_group_allowed(grp):
            continue
        sch, tbl = _resolve_schema_table(csv_name, table_name, _registry_flat, _known_schemas)
        _processed_keys.add((sch, tbl))
        _register_cdc_table(
            csv_name, csv_path, _registry_flat, _known_schemas,
            _evolved_schemas, _schema_changed, _cdc_base,
        )
else:
    _project_cdc_root = resolve_project_cdc_root(_cdc_base)
    if not _project_cdc_root:
        _emit('WARN', 'No CDC project root (acc.dc_cdc_path) — no CDC tables registered')
    for _csv in discover_cdc_csv_basenames(_project_cdc_root):
        _tbl = _table_name_from_csv(_csv)
        grp = cdc_group_for_csv_stem(_tbl)
        if not grp or not cdc_service_group_allowed(grp):
            continue
        _flat = _tbl.lower()
        if _flat in _registry_flat:
            _processed_keys.add(
                (_registry_flat[_flat]['schema'], _registry_flat[_flat]['table']),
            )
        else:
            _sch, _t = _split_csv_stem(_tbl, _known_schemas)
            _processed_keys.add((_sch, _t))
        _register_cdc_table(
            _csv,
            resolve_cdc_csv_glob(_project_cdc_root, _csv),
            _registry_flat,
            _known_schemas,
            _evolved_schemas,
            _schema_changed,
            _cdc_base,
        )

_sweep_heartbeat(_processed_keys)
emit_summary()
