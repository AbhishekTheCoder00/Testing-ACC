# Databricks notebook source
# acc-connector — Bronze AUTO CDC FROM SNAPSHOT pipeline.
#
# Reads CSV snapshots that the connector lands in
#   /Volumes/<catalog>/bronze/acc_bronze_volume/data_connector/<project>/<run>/*.csv
# sync_service passes ``acc.dc_snapshot_path`` so only that run folder is read
# (not a union of all historical runs). Manual runs fall back to latest run.
# and applies AUTO CDC FROM SNAPSHOT per CSV to a SCD-Type-2 Bronze Delta
# table. The pipeline detects deletions in two ways:
#   1. Hard deletes — row absent from the new snapshot.
#   2. Soft deletes — row present with ``deleted_at`` set; tombstoned via
#      the ``deleted_at IS NULL`` filter on the source view, which makes
#      the row appear absent to the AUTO CDC engine.
#
# Primary keys come from <catalog>.bronze._meta_bronze_pk_registry, which
# bootstrap.py seeds from schema.json with PK = column at ordinal_position 1.
# Per-table outcomes (clean sync, schema drift, skip on PK missing, etc.)
# are written to <catalog>.bronze._meta_bronze_table_status. Both surfaces
# plus a single end-of-run [SUMMARY] line are documented in
# SCHEMA_HANDLING_AND_EVOLUTION.md.

import hashlib
import io
import json
import re
import time
import zipfile

import dlt
from delta.tables import DeltaTable
from pyspark.sql import functions as F
from pyspark.sql.types import (
    ArrayType, StringType, StructField, StructType,
    IntegerType, LongType, DoubleType, BooleanType, TimestampType, DateType,
)


# ---------------------------------------------------------------------------
# Pipeline configuration. Set on the Pipeline definition under
# ``configuration`` — bootstrap passes ``acc.catalog`` so the same notebook
# works for every customer's catalog without per-tenant copies.
# ---------------------------------------------------------------------------
CATALOG       = spark.conf.get('acc.catalog', 'final_poc')
SCHEMA_BRONZE = 'bronze'
VOLUME_BASE   = f'/Volumes/{CATALOG}/{SCHEMA_BRONZE}/acc_bronze_volume'
DC_BASE       = f'{VOLUME_BASE}/data_connector'

# Set per sync by sync_service (Phase 3) to the folder just uploaded, e.g.
#   .../data_connector/<project_id>/2026-05-27T12-00-00
# When empty (manual pipeline run), _resolve_snapshot_base() picks the
# lexicographically latest run directory under acc.dc_project_id or all projects.
DC_SNAPSHOT_PATH = spark.conf.get('acc.dc_snapshot_path', '').strip()
DC_PROJECT_ID    = spark.conf.get('acc.dc_project_id', '').strip()
_DC_EXTRACT_ZIP_NAME = 'autodesk_data_extract.zip'

# Registry / status table FQNs. Kept in sync with backend/bootstrap.py.
META_REGISTRY_TABLE        = '_meta_bronze_pk_registry'
META_STATUS_TABLE          = '_meta_bronze_table_status'
META_SCHEMA_VERSIONS_TABLE = '_meta_bronze_schema_versions'
CANONICAL_SCHEMA_PATH      = f'{VOLUME_BASE}/schema.json'
FQN_REGISTRY = f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_REGISTRY_TABLE}`'
FQN_STATUS   = f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_STATUS_TABLE}`'
FQN_SCHEMA_VERSIONS = (
    f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_SCHEMA_VERSIONS_TABLE}`'
)

# Unquoted names for DeltaTable.forName / spark.table (SDP blocks MERGE/INSERT
# in spark.sql but allows DeltaTable.merge and DataFrame writes).
_TABLE_REGISTRY = f'{CATALOG}.{SCHEMA_BRONZE}.{META_REGISTRY_TABLE}'
_TABLE_STATUS = f'{CATALOG}.{SCHEMA_BRONZE}.{META_STATUS_TABLE}'
_TABLE_SCHEMA_VERSIONS = f'{CATALOG}.{SCHEMA_BRONZE}.{META_SCHEMA_VERSIONS_TABLE}'
_MERGE_KEYS_CATALOG_SCHEMA_TABLE = (
    'target.catalog = source.catalog AND '
    'target.`schema` = source.`schema` AND '
    'target.`table` = source.`table`'
)
_COLUMN_DIFF_TYPE = StructType([
    StructField('added', ArrayType(StringType())),
    StructField('absent_in_csv', ArrayType(StringType())),
    StructField('type_widened', ArrayType(StringType())),
    StructField('type_kept_old', ArrayType(StringType())),
])

# Run identifier for the [SUMMARY] line — tied to the Spark application so
# operators can correlate logs back to the Databricks run page.
RUN_ID = spark.conf.get("spark.databricks.clusterUsageTags.runId", "unknown")
RUN_STARTED_AT = time.time()

# Status enum (mirrors Section 8 of SCHEMA_HANDLING_AND_EVOLUTION.md).
STATUS_OK                   = 'ok'
STATUS_PK_MISSING_IN_CSV    = 'pk_missing_in_csv'
STATUS_CSV_UNREADABLE       = 'csv_unreadable'
STATUS_TYPE_INCOMPAT        = 'type_incompat'        # not raised at register time; reserved
STATUS_SKIPPED_NO_CSV       = 'skipped_no_csv'       # not raised here; emitted by status sweep below
STATUS_NOT_IN_SCHEMA_JSON   = 'not_in_schema_json'   # registry has no row and stem can't be split

# Aggregated counters for the end-of-run [SUMMARY] line.
_run_counts: dict[str, int] = {
    STATUS_OK:                 0,
    STATUS_PK_MISSING_IN_CSV:  0,
    STATUS_CSV_UNREADABLE:     0,
    STATUS_TYPE_INCOMPAT:      0,
    STATUS_SKIPPED_NO_CSV:     0,
    STATUS_NOT_IN_SCHEMA_JSON: 0,
    'schema_changes':          0,   # incremented when column_diff is non-empty on an OK
    'newly_discovered':        0,   # incremented when a new (schema, table) was INSERTed
}


# ---------------------------------------------------------------------------
# Logging helpers — single emission shape per Section 8 of the design doc.
# ---------------------------------------------------------------------------
def _emit(level: str, message: str) -> None:
    print(f'[{level}] {message}')


def _sql_str(s: str) -> str:
    """Sanitize a STRING literal for inline SQL."""
    return s.replace("'", "''")


def _sql_array(items) -> str:
    """Render a Python sequence as a Spark SQL array(...) literal of strings."""
    if not items:
        return 'array()'
    return 'array(' + ', '.join(f"'{_sql_str(str(x))}'" for x in items) + ')'


def build_target_schema(table_def: dict) -> StructType:
    """Maps schema.json data_type to PySpark types.
    
    Args:
        table_def: dict of {column_name: {data_type, ordinal_position, ...}}
    
    Returns:
        StructType with columns ordered by ordinal_position, all nullable
    """
    type_map = {
        'string':         StringType(),
        'string: uuid':   StringType(),
        'string: null':   StringType(),
        'timestamp: sql': TimestampType(),
        'number':         DoubleType(),
        'integer':        IntegerType(),
        'long':           LongType(),
        'double':         DoubleType(),
        'boolean':        BooleanType(),
        'enum: string':   StringType(),
        'date: string':   DateType(),
        'time':           StringType(),
    }
    
    sorted_cols = sorted(
        table_def.items(),
        key=lambda kv: kv[1].get('ordinal_position', 9999) 
                       if isinstance(kv[1], dict) else 9999
    )
    
    fields = []
    for col_name, col_meta in sorted_cols:
        if not isinstance(col_meta, dict):
            continue
        data_type_str = col_meta.get('data_type', 'string').lower()
        spark_type = type_map.get(data_type_str, StringType())
        fields.append(StructField(col_name, spark_type, nullable=True))
    
    return StructType(fields)


def _schema_for_scd2(evolved_struct: StructType | None) -> StructType | None:
    """Append SCD2 system columns for create_streaming_table declared schema."""
    if evolved_struct is None:
        return None
    return StructType(
        list(evolved_struct.fields) + [
            StructField('__START_AT', TimestampType(), nullable=True),
            StructField('__END_AT', TimestampType(), nullable=True),
        ]
    )


# ---------------------------------------------------------------------------
# Schema evolution helpers — reconcile target vs schema.json types.
# ---------------------------------------------------------------------------
SAFE_WIDENINGS: set[tuple[str, str]] = {
    ('int', 'bigint'), ('int', 'double'), ('int', 'float'),
    ('bigint', 'double'),
    ('float', 'double'),
    ('smallint', 'int'), ('smallint', 'bigint'), ('smallint', 'double'),
    ('tinyint', 'smallint'), ('tinyint', 'int'), ('tinyint', 'bigint'),
}


def _parse_catalog_type(type_str: str):
    """Parse a catalog type string (simpleString form) into a PySpark DataType."""
    from pyspark.sql.types import _parse_datatype_string
    try:
        return _parse_datatype_string(type_str)
    except Exception:
        return StringType()


def _get_target_schema(table_name: str) -> dict | None:
    """Return {col_name: simpleString_type} for an existing Bronze table, or None."""
    try:
        fqn = f'{CATALOG}.{SCHEMA_BRONZE}.{table_name}'
        cols = spark.catalog.listColumns(fqn)
        return {c.name: c.dataType for c in cols}
    except Exception:
        return None


def _build_evolved_schema(
    forma_struct: StructType, table_name: str, sch: str, tbl: str,
) -> tuple[StructType, dict]:
    """Build a reconciled schema merging target types with schema.json changes.

    Rules (non-destructive):
      - New column (in schema.json, not in target): add with schema.json type
      - Removed column (in target, not in schema.json): keep with target type
      - Safe widening (int->bigint etc.): use new wider type
      - Unsafe type change: keep target's old type (PERMISSIVE handles values)
    """
    column_diff = {
        'added': [], 'absent_in_csv': [], 'type_widened': [], 'type_kept_old': [],
    }

    existing = _get_target_schema(table_name)
    if existing is None:
        return forma_struct, column_diff

    fields: list[StructField] = []
    forma_names = set(forma_struct.fieldNames())

    for field in forma_struct.fields:
        new_simple = field.dataType.simpleString()
        if field.name not in existing:
            fields.append(StructField(field.name, field.dataType, nullable=True))
            column_diff['added'].append(field.name)
            _emit('INFO', f'{sch}.{tbl}: new column "{field.name}" '
                  f'({new_simple}) will be added')
        else:
            old_simple = existing[field.name]
            if old_simple == new_simple:
                fields.append(StructField(field.name, field.dataType, nullable=True))
            elif (old_simple, new_simple) in SAFE_WIDENINGS:
                fields.append(StructField(field.name, field.dataType, nullable=True))
                column_diff['type_widened'].append(
                    f'{field.name}: {old_simple} -> {new_simple}')
                _emit('INFO', f'{sch}.{tbl}: column "{field.name}" '
                      f'widened {old_simple} -> {new_simple}')
            else:
                old_spark_type = _parse_catalog_type(old_simple)
                fields.append(StructField(field.name, old_spark_type, nullable=True))
                column_diff['type_kept_old'].append(
                    f'{field.name}: schema.json wants {new_simple}, '
                    f'keeping {old_simple}')
                _emit('WARN', f'{sch}.{tbl}: UNSAFE type change for '
                      f'"{field.name}" ({old_simple} -> {new_simple}) '
                      f'— keeping old type, incompatible values become NULL')

    for col_name, col_type in existing.items():
        if col_name not in forma_names:
            spark_type = _parse_catalog_type(col_type)
            fields.append(StructField(col_name, spark_type, nullable=True))
            column_diff['absent_in_csv'].append(col_name)

    if column_diff['absent_in_csv']:
        _emit('WARN', f'{sch}.{tbl}: columns {column_diff["absent_in_csv"]} '
              f'removed from schema.json — keeping in target, will fill NULL')

    return StructType(fields), column_diff


def _build_all_evolved_schemas(
    typed_schemas: dict, schema_changed: bool, known_schemas: set,
) -> dict:
    """Build evolved schemas for all tables. Hash-gated: no-op when unchanged."""
    if not schema_changed:
        return typed_schemas

    evolved = {}
    for key, forma_struct in typed_schemas.items():
        sch, tbl = _split_csv_stem(key, known_schemas)
        table_name = key
        evolved_struct, _ = _build_evolved_schema(
            forma_struct, table_name, sch, tbl)
        evolved[key] = evolved_struct
    return evolved


def _delta_merge_registry_rows(rows: list[tuple[str, str, str]]) -> None:
    """Insert-only registry rows via DeltaTable (SDP-safe)."""
    if not rows:
        return
    source_df = (
        spark.createDataFrame(
            [(CATALOG, s, t, [c], 'auto_position_1') for s, t, c in rows],
            ['catalog', 'schema', 'table', 'pk_columns', 'source'],
        )
        .withColumn('locked_at', F.current_timestamp())
    )
    (
        DeltaTable.forName(spark, _TABLE_REGISTRY)
        .alias('target')
        .merge(source_df.alias('source'), _MERGE_KEYS_CATALOG_SCHEMA_TABLE)
        .whenNotMatchedInsertAll()
        .execute()
    )


def _delta_upsert_config_version(
    config_type: str, config_hash: str, config_path: str
) -> None:
    """Track pk_config or schema_json hash in _meta_bronze_schema_versions.
    
    Args:
        config_type: 'pk_config' or 'schema_json'
        config_hash: SHA-256 hash of the config file
        config_path: Volume path to the config file
    """
    project_id = DC_PROJECT_ID or 'unknown'
    source_df = (
        spark.createDataFrame(
            [(CATALOG, project_id, config_type, config_hash, config_path)],
            ['catalog', 'project_id', 'config_type', 'config_hash', 'config_path']
        )
        .withColumn('last_applied_at', F.current_timestamp())
    )
    (
        DeltaTable.forName(spark, _TABLE_SCHEMA_VERSIONS)
        .alias('target')
        .merge(
            source_df.alias('source'),
            'target.catalog = source.catalog AND '
            'target.project_id = source.project_id AND '
            'target.config_type = source.config_type'
        )
        .whenMatchedUpdate(set={
            'config_hash': 'source.config_hash',
            'last_applied_at': 'source.last_applied_at',
            'config_path': 'source.config_path',
        })
        .whenNotMatchedInsertAll()
        .execute()
    )


_PK_CONFIG_UPDATABLE_SOURCES = frozenset({'auto_position_1', 'explicit_pk_config'})
_PK_CONFIG_MERGE_UPDATE_CONDITION = (
    "target.source IN ('auto_position_1', 'explicit_pk_config') AND "
    "(target.pk_columns != source.pk_columns OR target.source != source.source)"
)


def _seed_pk_registry_from_config() -> None:
    """Sync registry from pk_config.json when file hash changes (upsert).

    Hash unchanged and registry non-empty → no-op. Otherwise INSERT new rows
    and UPDATE pk_columns for auto_position_1 / explicit_pk_config only.
    manual_override and expert_curated rows are never overwritten.
    """
    pk_config_path = f'{VOLUME_BASE}/pk_config.json'

    if not _fs_exists(pk_config_path):
        _emit('WARN', 'pk_config.json not found - skipping PK registry sync')
        return

    pk_config_bytes = _read_volume_bytes(pk_config_path)
    pk_config = json.loads(pk_config_bytes.decode('utf-8'))
    pk_hash = hashlib.sha256(pk_config_bytes).hexdigest()

    registry_count = 0
    try:
        registry_count = int(spark.sql(
            f'SELECT COUNT(*) AS n FROM {FQN_REGISTRY} '
            f"WHERE catalog = '{_sql_str(CATALOG)}'"
        ).collect()[0]['n'])
    except Exception:
        pass

    stored_hash = None
    try:
        prev = spark.sql(
            f'SELECT config_hash FROM {FQN_SCHEMA_VERSIONS} '
            f"WHERE catalog = '{_sql_str(CATALOG)}' "
            f"  AND config_type = 'pk_config'"
        ).collect()
        if prev:
            stored_hash = prev[0]['config_hash']
    except Exception:
        pass

    if stored_hash == pk_hash and registry_count > 0:
        _emit('INFO', 'pk_config.json unchanged - PK registry sync skipped')
        return

    rows: list[tuple[str, str, list]] = []
    for schema_name, tables in pk_config.items():
        if not isinstance(tables, dict):
            continue
        for table_name, pk_cols in tables.items():
            if isinstance(pk_cols, list) and pk_cols:
                rows.append((schema_name, table_name, list(pk_cols)))

    if not rows:
        _emit('WARN', 'pk_config.json yielded zero tables')
        return

    existing: dict[tuple[str, str], dict] = {}
    try:
        for r in spark.sql(
            f'SELECT `schema`, `table`, pk_columns, source FROM {FQN_REGISTRY} '
            f"WHERE catalog = '{_sql_str(CATALOG)}'"
        ).collect():
            existing[(r['schema'], r['table'])] = {
                'pk_columns': list(r['pk_columns'] or []),
                'source': r['source'],
            }
    except Exception:
        pass

    config_keys: set[tuple[str, str]] = set()
    n_insert = n_update = n_unchanged = 0
    for sch, tbl, pk_cols in rows:
        key = (sch, tbl)
        config_keys.add(key)
        norm_new = sorted(pk_cols)
        if key not in existing:
            n_insert += 1
            continue
        ex = existing[key]
        if ex['source'] not in _PK_CONFIG_UPDATABLE_SOURCES:
            n_unchanged += 1
            continue
        norm_old = sorted(ex['pk_columns'])
        if norm_old != norm_new:
            n_update += 1
            _emit(
                'WARN',
                f'{sch}.{tbl}: PK updated {ex["pk_columns"]} -> {pk_cols} '
                f'— rebuild Bronze if already ingested',
            )
        elif ex['source'] != 'explicit_pk_config':
            n_update += 1
        else:
            n_unchanged += 1

    n_orphan = sum(1 for k in existing if k not in config_keys)
    if n_orphan:
        _emit(
            'INFO',
            f'{n_orphan} registry row(s) not listed in pk_config.json '
            f'(left unchanged)',
        )

    for i in range(0, len(rows), _REGISTRY_SEED_BATCH):
        batch = rows[i:i + _REGISTRY_SEED_BATCH]
        source_df = (
            spark.createDataFrame(
                [(CATALOG, s, t, pk_cols, 'explicit_pk_config') for s, t, pk_cols in batch],
                ['catalog', 'schema', 'table', 'pk_columns', 'source'],
            )
            .withColumn('locked_at', F.current_timestamp())
        )
        (
            DeltaTable.forName(spark, _TABLE_REGISTRY)
            .alias('target')
            .merge(source_df.alias('source'), _MERGE_KEYS_CATALOG_SCHEMA_TABLE)
            .whenMatchedUpdate(
                condition=_PK_CONFIG_MERGE_UPDATE_CONDITION,
                set={
                    'pk_columns': 'source.pk_columns',
                    'source': 'source.source',
                    'locked_at': 'source.locked_at',
                },
            )
            .whenNotMatchedInsertAll()
            .execute()
        )

    _delta_upsert_config_version('pk_config', pk_hash, pk_config_path)
    _emit(
        'INFO',
        f'PK registry synced from pk_config.json ({len(rows)} tables, '
        f'{n_insert} inserted, {n_update} updated)',
    )


# ---------------------------------------------------------------------------
# Registry / volume access.
# ---------------------------------------------------------------------------
def _load_registry() -> tuple[dict, set]:
    """Load _meta_bronze_pk_registry into:
      flat_lookup: { '<schema>_<table>'.lower() -> {schema, table, pk_columns} }
      known_schemas: set of distinct schema names (for stem splitting)
    Both empty if the registry table is missing or empty.
    """
    flat: dict[str, dict] = {}
    schemas: set[str] = set()
    try:
        df = spark.sql(
            f'SELECT `schema`, `table`, pk_columns FROM {FQN_REGISTRY}'
        )
        for row in df.collect():
            sch = row['schema']
            tbl = row['table']
            pks = list(row['pk_columns'] or [])
            flat[f'{sch}_{tbl}'.lower()] = {
                'schema':     sch,
                'table':      tbl,
                'pk_columns': pks,
            }
            schemas.add(sch)
    except Exception as exc:
        _emit('WARN', f'registry read failed: {exc}; treating as empty')
    return flat, schemas


def _split_csv_stem(stem: str, known_schemas: set) -> tuple[str, str]:
    """Best-effort split of a CSV stem into (schema, table).

    Picks the longest known schema name `s` such that `stem` starts with
    `s + '_'`. Falls back to ('unknown', stem) when nothing matches —
    that case feeds into status='not_in_schema_json'.
    """
    candidates = sorted(
        (s for s in known_schemas if stem.startswith(s + '_')),
        key=len,
        reverse=True,
    )
    if candidates:
        sch = candidates[0]
        return sch, stem[len(sch) + 1:]
    return 'unknown', stem


def _read_csv_header(glob_path: str) -> list[str]:
    """Return the column names from CSVs matching glob_path.

    Uses Spark's CSV reader with header=True, limit(0) — Spark scans
    enough of one matching file to infer the schema without reading
    the data. Raises FileNotFoundError if no file matches.
    """
    try:
        df = (spark.read.format('csv')
              .option('header', True)
              .option('inferSchema', False)
              .load(glob_path)
              .limit(0))
        cols = df.columns
        if not cols:
            raise ValueError(f'CSV header empty at {glob_path}')
        return cols
    except Exception as exc:
        msg = str(exc)
        if 'Path does not exist' in msg or 'does not exist' in msg.lower():
            raise FileNotFoundError(f'no files match {glob_path}') from exc
        raise


def _existing_delta_columns(table_name: str) -> set:
    """Return the set of columns in the existing Bronze Delta table for
    `table_name`, or empty set if the table does not yet exist.
    """
    try:
        df = spark.sql(
            f'SHOW COLUMNS IN `{CATALOG}`.`{SCHEMA_BRONZE}`.`{table_name}`'
        )
        return {row['col_name'] for row in df.collect()}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Status writing — single MERGE per table per run.
# ---------------------------------------------------------------------------
def _record_status(
    schema_name: str,
    table_name: str,
    status: str,
    csv_header: list,
    error_msg: str | None = None,
    column_diff: dict | None = None,
) -> None:
    """MERGE one row into _meta_bronze_table_status. Inserts the row if
    new; otherwise updates last_synced_at, last_run_status, and the
    debugging fields. last_synced_at is bumped on EVERY call regardless
    of status — that's the heartbeat invariant from the design doc.
    """
    column_diff = column_diff or {
        'added': [], 'absent_in_csv': [], 'type_widened': [], 'type_kept_old': [],
    }
    try:
        source_df = spark.createDataFrame(
            [(
                CATALOG,
                schema_name,
                table_name,
                status,
                error_msg,
                list(csv_header or []),
                (
                    column_diff.get('added', []),
                    column_diff.get('absent_in_csv', []),
                    column_diff.get('type_widened', []),
                    column_diff.get('type_kept_old', []),
                ),
            )],
            StructType([
                StructField('catalog', StringType()),
                StructField('schema', StringType()),
                StructField('table', StringType()),
                StructField('last_run_status', StringType()),
                StructField('last_error_message', StringType()),
                StructField('csv_header_at_last_run', ArrayType(StringType())),
                StructField('column_diff', _COLUMN_DIFF_TYPE),
            ]),
        ).withColumn('last_synced_at', F.current_timestamp())
        (
            DeltaTable.forName(spark, _TABLE_STATUS)
            .alias('target')
            .merge(source_df.alias('source'), _MERGE_KEYS_CATALOG_SCHEMA_TABLE)
            .whenMatchedUpdate(set={
                'last_synced_at': 'source.last_synced_at',
                'last_run_status': 'source.last_run_status',
                'last_error_message': 'source.last_error_message',
                'csv_header_at_last_run': 'source.csv_header_at_last_run',
                'column_diff': 'source.column_diff',
            })
            .whenNotMatchedInsertAll()
            .execute()
        )
    except Exception as exc:
        _emit('WARN', f'status MERGE failed for {schema_name}.{table_name}: {exc}')


def _insert_registry_row(
    schema_name: str, table_name: str, pk_col: str,
) -> None:
    """Insert a brand-new (catalog, schema, table) row into the PK registry.
    Used when a CSV arrives for a table that wasn't seeded at bootstrap
    (e.g., Autodesk added a table after our last bootstrap run).
    """
    try:
        _delta_merge_registry_rows([(schema_name, table_name, pk_col)])
        _run_counts['newly_discovered'] += 1
    except Exception as exc:
        _emit('WARN', f'registry INSERT failed for {schema_name}.{table_name}: {exc}')


# ---------------------------------------------------------------------------
# CSV discovery and per-table registration.
# ---------------------------------------------------------------------------
def _table_name_from_csv(filename: str) -> str:
    """Convert ``admin_users.csv`` → ``admin_users`` (lowercased, sanitized)."""
    stem = filename[:-4] if filename.lower().endswith('.csv') else filename
    return re.sub(r'[^a-z0-9_]', '_', stem.lower())


def _resolve_snapshot_base() -> str:
    """Directory containing this sync's DC CSV snapshot (one run folder).

    Prefer ``acc.dc_snapshot_path`` from the pipeline update configuration
    (set by sync_service). Otherwise choose the latest run folder under
    ``data_connector/`` so ad-hoc pipeline runs still work.
    """
    if DC_SNAPSHOT_PATH:
        return DC_SNAPSHOT_PATH.rstrip('/')

    try:
        if DC_PROJECT_ID:
            project_dirs = [f'{DC_BASE}/{DC_PROJECT_ID}']
        else:
            project_dirs = [e.path for e in dbutils.fs.ls(DC_BASE) if e.isDir()]
    except Exception:
        return ''

    latest_path = ''
    latest_key = ''
    for proj_dir in project_dirs:
        try:
            run_dirs = [e.path for e in dbutils.fs.ls(proj_dir) if e.isDir()]
        except Exception:
            continue
        for run_dir in run_dirs:
            name = run_dir.rstrip('/').split('/')[-1]
            if name > latest_key:
                latest_key = name
                latest_path = run_dir.rstrip('/')
    if latest_path:
        _emit('INFO', f'snapshot base (latest run): {latest_path}')
    return latest_path


def _discover_csv_basenames(snapshot_base: str) -> list[str]:
    """Return CSV file names in ``snapshot_base`` (one DC export folder)."""
    if not snapshot_base:
        return []
    seen: set[str] = set()
    try:
        entries = dbutils.fs.ls(snapshot_base)
    except Exception:
        return []
    for entry in entries:
        if entry.name.lower().endswith('.csv'):
            seen.add(entry.name)
    return sorted(seen)



def _register_table(
    csv_name: str,
    registry: dict,
    known_schemas: set,
    snapshot_base: str,
    evolved_schemas: dict,
    schema_changed: bool,
) -> None:
    """Per-CSV: gate on PK presence in header, build reconciled schema,
    register AUTO CDC flow, and record outcome in _meta_bronze_table_status.
    """
    table_name = _table_name_from_csv(csv_name)
    if not snapshot_base:
        sch, tbl = _resolve_schema_table(
            csv_name, table_name, registry, known_schemas,
        )
        _emit('ERROR', f'{sch}.{tbl}: skipped — no DC snapshot directory resolved')
        _record_status(sch, tbl, STATUS_SKIPPED_NO_CSV, csv_header=[],
                       error_msg='acc.dc_snapshot_path unset and no run folder found')
        _run_counts[STATUS_SKIPPED_NO_CSV] += 1
        return
    glob_path = f'{snapshot_base}/{csv_name}'

    # 1. CSV header read — also doubles as a "is this CSV readable?" check.
    try:
        header = _read_csv_header(glob_path)
    except FileNotFoundError:
        sch, tbl = _resolve_schema_table(csv_name, table_name, registry, known_schemas)
        _emit('WARN', f'{sch}.{tbl}: skipped — no CSV file in volume for this table this run')
        _record_status(sch, tbl, STATUS_SKIPPED_NO_CSV, csv_header=[],
                       error_msg='no matching CSV file at glob')
        _run_counts[STATUS_SKIPPED_NO_CSV] += 1
        return
    except Exception as exc:
        sch, tbl = _resolve_schema_table(csv_name, table_name, registry, known_schemas)
        _emit('ERROR', f'{sch}.{tbl}: skipped — CSV unreadable or empty ({exc})')
        _record_status(sch, tbl, STATUS_CSV_UNREADABLE, csv_header=[],
                       error_msg=str(exc))
        _run_counts[STATUS_CSV_UNREADABLE] += 1
        return

    # 2. Resolve to (schema, table).
    flat_key = table_name.lower()
    if flat_key in registry:
        sch        = registry[flat_key]['schema']
        tbl        = registry[flat_key]['table']
        pk_columns = registry[flat_key]['pk_columns']
    else:
        sch, tbl = _split_csv_stem(table_name, known_schemas)
        if sch == 'unknown':
            _emit('WARN', f'unknown.{tbl}: skipped — no registry entry and stem does not match any known schema')
            _record_status(sch, tbl, STATUS_NOT_IN_SCHEMA_JSON, csv_header=header,
                           error_msg='registry has no row; stem does not match any known schema')
            _run_counts[STATUS_NOT_IN_SCHEMA_JSON] += 1
            return
        pk_col = header[0] if header else None
        if not pk_col:
            _emit('ERROR', f'{sch}.{tbl}: skipped — empty CSV header')
            _record_status(sch, tbl, STATUS_CSV_UNREADABLE, csv_header=header,
                           error_msg='empty header')
            _run_counts[STATUS_CSV_UNREADABLE] += 1
            return
        _insert_registry_row(sch, tbl, pk_col)
        pk_columns = [pk_col]
        _emit('INFO', f'new table {sch}.{tbl} discovered; PK locked as {pk_columns} (source=auto_position_1)')

    # 3. Gate: every PK column must be present in the CSV header.
    header_set = set(header)
    missing = [c for c in pk_columns if c not in header_set]
    if missing:
        _emit('ERROR',
              f'{sch}.{tbl}: skipped — required PK column(s) {missing} '
              f'missing from CSV header. Actual header: {header}')
        _record_status(sch, tbl, STATUS_PK_MISSING_IN_CSV, csv_header=header,
                       error_msg=f'PK column(s) {missing} missing from CSV header')
        _run_counts[STATUS_PK_MISSING_IN_CSV] += 1
        return

    # 4. Get evolved schema and compute column_diff.
    forma_key = f'{sch}_{tbl}'.lower()
    evolved_struct = evolved_schemas.get(forma_key)

    if schema_changed and evolved_struct is not None:
        _, evo_diff = _build_evolved_schema(evolved_struct, table_name, sch, tbl)
        column_diff = evo_diff
        if any(v for v in column_diff.values() if v):
            _run_counts['schema_changes'] += 1
    else:
        column_diff = {
            'added': [], 'absent_in_csv': [], 'type_widened': [], 'type_kept_old': [],
        }

    # 5. Register the AUTO CDC flow with reconciled schema + DLT expectations.
    view_name = f'v_{table_name}'
    pk_expect_expr = ' AND '.join(f'`{c}` IS NOT NULL' for c in pk_columns)

    @dlt.view(name=view_name)
    @dlt.expect('pk_not_null', pk_expect_expr)
    def _src(_glob=glob_path, _evolved=evolved_struct):
        if _evolved is not None:
            _emit('INFO', f'{sch}.{tbl}: using evolved schema ({len(_evolved.fields)} cols)')
            df = (spark.read.format('csv')
                  .schema(_evolved)
                  .option('header', True)
                  .option("multiLine", True)
                  .option('mode', 'PERMISSIVE')
                  .option('nullValue', '')
                  .option('emptyValue', '')
                  .csv(_glob))
        else:
            _emit('WARN', f'{sch}.{tbl}: no typed schema — using string fallback')
            df = (spark.read.format('csv')
                  .option('header', True)
                  .option("multiLine", True)
                  .option('inferSchema', False)
                  .csv(_glob))
        if 'deleted_at' in df.columns:
            df = df.filter(F.col('deleted_at').isNull())
        return df

    dlt.create_streaming_table(
        name=table_name,
        schema=_schema_for_scd2(evolved_struct),
        comment=f'ACC Data Connector snapshot -> SCD2 Bronze ({csv_name}). PK: {pk_columns}',
    )
    dlt.create_auto_cdc_from_snapshot_flow(
        target=table_name,
        source=view_name,
        keys=pk_columns,
        stored_as_scd_type=2,
    )

    # 6. Record success and emit per-table OK line.
    _record_status(sch, tbl, STATUS_OK, csv_header=header, column_diff=column_diff)
    _run_counts[STATUS_OK] += 1
    _emit('OK', f'{sch}.{tbl}: registered AUTO CDC flow, keys={pk_columns}')


def _resolve_schema_table(
    csv_name: str, table_name: str, registry: dict, known_schemas: set,
) -> tuple[str, str]:
    """Best-effort (schema, table) resolution for log/status writes when
    we couldn't otherwise process the CSV. Mirrors _register_table's
    classification logic so status rows are consistently keyed."""
    flat_key = table_name.lower()
    if flat_key in registry:
        return registry[flat_key]['schema'], registry[flat_key]['table']
    return _split_csv_stem(table_name, known_schemas)


# ---------------------------------------------------------------------------
# Heartbeat sweep — every (schema, table) registry entry that did not get
# a status row written this run gets one with status=skipped_no_csv. This
# is the "heartbeat on every run" guarantee from the design doc; no row
# is left with a stale last_synced_at.
# ---------------------------------------------------------------------------
def _sweep_heartbeat(processed_keys: set) -> None:
    """Bump last_synced_at = now() on every registry row not in processed_keys,
    setting status='skipped_no_csv' for those that didn't have a CSV this run.
    """
    try:
        registry_df = spark.table(_TABLE_REGISTRY).filter(
            F.col('catalog') == CATALOG
        )
        if processed_keys:
            processed_df = spark.createDataFrame(
                [(CATALOG, s, t) for s, t in processed_keys],
                ['catalog', 'schema', 'table'],
            )
            registry_df = registry_df.join(
                processed_df,
                ['catalog', 'schema', 'table'],
                'left_anti',
            )
        source_df = registry_df.select(
            F.col('catalog'),
            F.col('schema'),
            F.col('table'),
            F.current_timestamp().alias('last_synced_at'),
            F.lit(STATUS_SKIPPED_NO_CSV).alias('last_run_status'),
            F.lit(
                'registry entry exists but no CSV in this run'
            ).alias('last_error_message'),
            F.array().cast(ArrayType(StringType())).alias('csv_header_at_last_run'),
            F.struct(
                F.array().cast(ArrayType(StringType())).alias('added'),
                F.array().cast(ArrayType(StringType())).alias('absent_in_csv'),
                F.array().cast(ArrayType(StringType())).alias('type_widened'),
                F.array().cast(ArrayType(StringType())).alias('type_kept_old'),
            ).alias('column_diff'),
        )
        if source_df.limit(1).count() == 0:
            return
        (
            DeltaTable.forName(spark, _TABLE_STATUS)
            .alias('target')
            .merge(source_df.alias('source'), _MERGE_KEYS_CATALOG_SCHEMA_TABLE)
            .whenMatchedUpdate(set={
                'last_synced_at': 'source.last_synced_at',
                'last_run_status': 'source.last_run_status',
                'last_error_message': 'source.last_error_message',
            })
            .whenNotMatchedInsertAll()
            .execute()
        )
        n = source_df.count()
        if n > 0:
            _run_counts[STATUS_SKIPPED_NO_CSV] += n
    except Exception as exc:
        _emit('WARN', f'heartbeat sweep failed: {exc}')


# ---------------------------------------------------------------------------
# Canonical schema.json — bootstrap, hash-gated registry, forma types.
# ---------------------------------------------------------------------------
_REGISTRY_SEED_BATCH = 50


def _fs_exists(path: str) -> bool:
    try:
        dbutils.fs.ls(path)
        return True
    except Exception:
        return False


def _read_volume_bytes(path: str) -> bytes:
    rows = spark.read.format('binaryFile').load(path).collect()
    if not rows:
        raise FileNotFoundError(path)
    return bytes(rows[0]['content'])


def _write_volume_json(path: str, doc: dict) -> None:
    payload = json.dumps(doc, indent=2).encode('utf-8')
    dbutils.fs.put(path, payload.decode('utf-8'), overwrite=True)


def _schema_doc_hash(path: str) -> str:
    return hashlib.sha256(_read_volume_bytes(path)).hexdigest()


def _is_three_level_schema_doc(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    for sch_val in doc.values():
        if not isinstance(sch_val, dict):
            continue
        for tbl_val in sch_val.values():
            if not isinstance(tbl_val, dict):
                continue
            for col_val in tbl_val.values():
                return isinstance(col_val, dict) and 'ordinal_position' in col_val
    return False


def _is_two_level_schema_doc(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    for tbl_val in doc.values():
        if not isinstance(tbl_val, dict):
            continue
        for col_val in tbl_val.values():
            return isinstance(col_val, dict) and 'ordinal_position' in col_val
    return False


def _load_schema_doc_from_zip_bytes(zip_bytes: bytes) -> dict | None:
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            consolidated = [
                n for n in names
                if n.lower().endswith('/schema.json') or n.lower() == 'schema.json'
            ]
            for member in consolidated:
                with zf.open(member) as f:
                    doc = json.loads(f.read().decode('utf-8'))
                if _is_three_level_schema_doc(doc):
                    return doc
            merged: dict = {}
            per_domain = [
                n for n in names
                if '/schemas/' in n.lower() and n.lower().endswith('.json')
                and not n.lower().endswith('/schema.json')
            ]
            for member in per_domain:
                domain = member.rsplit('/', 1)[-1][:-5]
                with zf.open(member) as f:
                    doc = json.loads(f.read().decode('utf-8'))
                if _is_two_level_schema_doc(doc):
                    merged[domain] = doc
                elif _is_three_level_schema_doc(doc):
                    for sch, tbls in doc.items():
                        merged.setdefault(sch, tbls)
            return merged or None
    except Exception as exc:
        _emit('WARN', f'cannot read schema zip: {exc}')
        return None


def _merge_per_domain_schemas(schemas_dir: str) -> dict | None:
    merged: dict = {}
    try:
        entries = dbutils.fs.ls(schemas_dir)
    except Exception:
        return None
    for entry in entries:
        if entry.isDir() or not entry.name.lower().endswith('.json'):
            continue
        if entry.name.lower() == 'schema.json':
            continue
        domain = entry.name[:-5]
        try:
            doc = json.loads(_read_volume_bytes(entry.path).decode('utf-8'))
        except Exception:
            continue
        if _is_two_level_schema_doc(doc):
            merged[domain] = doc
    return merged or None


def _bootstrap_schema_json() -> str | None:
    """Extract schema.json from the current run's zip and update root only if content changed."""
    import hashlib as _hashlib

    snapshot_base = _resolve_snapshot_base()

    # Build candidate zip paths for this run
    zip_paths = []
    if snapshot_base:
        zip_paths.append(f'{snapshot_base}/{_DC_EXTRACT_ZIP_NAME}')
        try:
            parent = '/'.join(snapshot_base.rstrip('/').split('/')[:-1])
            zip_paths.append(f'{parent}/{_DC_EXTRACT_ZIP_NAME}')
        except Exception:
            pass

    fresh_doc = None
    used_zip = None
    for zp in zip_paths:
        if not _fs_exists(zp):
            continue
        doc = _load_schema_doc_from_zip_bytes(_read_volume_bytes(zp))
        if doc:
            fresh_doc = doc
            used_zip = zp
            break

    if fresh_doc is not None:
        import json as _json
        fresh_bytes = _json.dumps(fresh_doc, sort_keys=True).encode('utf-8')
        fresh_hash = _hashlib.sha256(fresh_bytes).hexdigest()

        existing_hash = None
        if _fs_exists(CANONICAL_SCHEMA_PATH):
            try:
                existing_hash = _hashlib.sha256(_read_volume_bytes(CANONICAL_SCHEMA_PATH)).hexdigest()
            except Exception:
                pass

        if existing_hash is None or existing_hash != fresh_hash:
            _write_volume_json(CANONICAL_SCHEMA_PATH, fresh_doc)
            _emit('INFO', f'schema.json updated at {CANONICAL_SCHEMA_PATH} from zip {used_zip}')
        else:
            _emit('INFO', f'schema.json unchanged — root not overwritten')
        return CANONICAL_SCHEMA_PATH

    # Fallback: if no zip found, use existing root if present
    if _fs_exists(CANONICAL_SCHEMA_PATH):
        _emit('INFO', f'No zip found — using existing {CANONICAL_SCHEMA_PATH}')
        return CANONICAL_SCHEMA_PATH

    # Last resort: merge per-domain schemas
    schemas_dir = f'{snapshot_base}/schemas' if snapshot_base else ''
    if schemas_dir and _fs_exists(schemas_dir):
        merged = _merge_per_domain_schemas(schemas_dir)
        if merged:
            _write_volume_json(CANONICAL_SCHEMA_PATH, merged)
            _emit('INFO', f'merged per-domain schemas into {CANONICAL_SCHEMA_PATH}')
            return CANONICAL_SCHEMA_PATH

    _emit('WARN', 'canonical schema.json not found — registry/forma validation skipped')
    return None


def _load_typed_schemas(schema_path: str | None) -> tuple[dict, bool]:
    """Build typed StructType per table from schema.json (hash-gated).

    Returns:
        (schemas_dict, schema_changed)
        schemas_dict: {f'{schema}_{table}'.lower(): StructType}
        schema_changed: True if hash differs from stored (or first run)
    """
    if not schema_path or not _fs_exists(schema_path):
        return {}, False

    schema_bytes = _read_volume_bytes(schema_path)
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()

    schema_changed = False
    try:
        prev = spark.sql(
            f"SELECT config_hash FROM {FQN_SCHEMA_VERSIONS} "
            f"WHERE catalog = '{_sql_str(CATALOG)}' "
            f"  AND config_type = 'schema_json'"
        ).collect()
        if prev and prev[0]['config_hash'] == schema_hash:
            schema_changed = False
            _emit('INFO', 'schema.json unchanged — skipping type-conflict checks')
        else:
            schema_changed = True
            _delta_upsert_config_version('schema_json', schema_hash, schema_path)
            _emit('INFO', 'schema.json CHANGED — evolution + drift detection will run')
    except Exception:
        schema_changed = True

    doc = json.loads(schema_bytes.decode('utf-8'))
    schemas = {}
    for sch_name, tables in doc.items():
        if not isinstance(tables, dict):
            continue
        for tbl_name, columns in tables.items():
            if not isinstance(columns, dict):
                continue
            try:
                schemas[f'{sch_name}_{tbl_name}'.lower()] = build_target_schema(columns)
            except Exception as e:
                _emit('WARN', f'Failed to build schema for {sch_name}.{tbl_name}: {e}')

    return schemas, schema_changed


def _apply_table_documentation() -> None:
    """Add Unity Catalog comments to meta tables for discoverability."""
    try:
        spark.sql(
            f"COMMENT ON TABLE {FQN_REGISTRY} IS "
            "'Primary key registry for ACC Bronze tables. Seeded from pk_config.json. "
            "Used by AUTO CDC FROM SNAPSHOT to identify rows across snapshots. "
            "Source: explicit_pk_config (from pk_config.json) or auto_position_1 (legacy).'"
        )
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN catalog COMMENT 'Customer catalog name (multi-tenant isolation)'")
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN `schema` COMMENT 'ACC service group (e.g., activities, admin, cost)'")
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN `table` COMMENT 'ACC table name within the service group'")
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN pk_columns COMMENT 'Primary key column(s) for this table — used by APPLY CHANGES FROM SNAPSHOT'")
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN source COMMENT 'How PK was determined: explicit_pk_config | auto_position_1 | manual_override'")
        spark.sql(f"ALTER TABLE {FQN_REGISTRY} ALTER COLUMN locked_at COMMENT 'Timestamp when this registry row was last inserted or updated'")

        spark.sql(
            f"COMMENT ON TABLE {FQN_STATUS} IS "
            "'Per-table ingestion status tracker. Records outcome of each sync run per table. "
            "Statuses: ok, pk_missing_in_csv, csv_unreadable, type_incompat, skipped_no_csv, not_in_schema_json.'"
        )
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN last_run_status COMMENT 'Outcome of most recent sync: ok | pk_missing_in_csv | csv_unreadable | type_incompat'")
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN column_diff COMMENT 'Schema drift details: {{added: [...], absent_in_csv: [...], type_widened: [...]}}'")
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN last_synced_at COMMENT 'Heartbeat — updated on every sync run regardless of status'")

        spark.sql(
            f"COMMENT ON TABLE {FQN_SCHEMA_VERSIONS} IS "
            "'Tracks schema.json and pk_config.json hashes per project. "
            "Used for change detection — pipeline skips re-processing if hash unchanged.'"
        )
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_type COMMENT 'Type of config: schema_json | pk_config'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_hash COMMENT 'SHA-256 hash of config file content — used for change detection'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_path COMMENT 'Volume path to the config file'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN last_applied_at COMMENT 'When this config was last processed/applied'")

        _emit('INFO', 'Table documentation (comments) applied to meta tables')
    except Exception as e:
        _emit('WARN', f'Table documentation skipped — {e}')


# ---------------------------------------------------------------------------
# Main — definition-time per-table loop, then heartbeat sweep, then summary.
# ---------------------------------------------------------------------------
# Phase 1a: PK registry from pk_config.json
_seed_pk_registry_from_config()
_apply_table_documentation()

# Phase 1b: Typed schemas from schema.json
_schema_path = _bootstrap_schema_json()
_typed_schemas, _schema_changed = _load_typed_schemas(_schema_path)

# Load PK registry for processing
_registry_flat, _known_schemas = _load_registry()
_snapshot_base = _resolve_snapshot_base()

# Phase 1c: Build evolved schemas (hash-gated — no-op when schema unchanged)
_evolved_schemas = _build_all_evolved_schemas(
    _typed_schemas, _schema_changed, _known_schemas)

_processed_keys: set[tuple[str, str]] = set()
for _csv in _discover_csv_basenames(_snapshot_base):
    _tbl = _table_name_from_csv(_csv)
    _flat = _tbl.lower()
    if _flat in _registry_flat:
        _processed_keys.add(
            (_registry_flat[_flat]['schema'], _registry_flat[_flat]['table'])
        )
    else:
        _sch, _t = _split_csv_stem(_tbl, _known_schemas)
        _processed_keys.add((_sch, _t))
    _register_table(
        _csv, _registry_flat, _known_schemas, _snapshot_base,
        _evolved_schemas, _schema_changed,
    )

_sweep_heartbeat(_processed_keys)

# ---------------------------------------------------------------------------
# End-of-run [SUMMARY] line — single aggregated signal.
# ---------------------------------------------------------------------------
_duration_s = int(time.time() - RUN_STARTED_AT)
_hh, _rem = divmod(_duration_s, 3600)
_mm, _ss  = divmod(_rem, 60)
_emit('SUMMARY',
      f'sync_run_id={RUN_ID} duration={_hh:02d}:{_mm:02d}:{_ss:02d}\n'
      f'  ingested_clean: {_run_counts[STATUS_OK]}\n'
      f'  schema_changes: {_run_counts["schema_changes"]}\n'
      f'  skipped_pk_missing: {_run_counts[STATUS_PK_MISSING_IN_CSV]}\n'
      f'  skipped_type_incompat: {_run_counts[STATUS_TYPE_INCOMPAT]}\n'
      f'  skipped_csv_unreadable: {_run_counts[STATUS_CSV_UNREADABLE]}\n'
      f'  skipped_no_csv: {_run_counts[STATUS_SKIPPED_NO_CSV]}\n'
      f'  skipped_not_in_schema_json: {_run_counts[STATUS_NOT_IN_SCHEMA_JSON]}\n'
      f'  newly_discovered: {_run_counts["newly_discovered"]}')
