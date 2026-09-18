# Databricks notebook source
# Shared ACC Bronze pipeline helpers — schema/PK/registry/bootstrap logic
# extracted from auto_cdc_pipeline.py for reuse across snapshot and CDC pipelines.

import hashlib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import dlt
from delta.tables import DeltaTable
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import (
    ArrayType, StringType, StructField, StructType,
    IntegerType, LongType, DoubleType, BooleanType, TimestampType, DateType,
)

# ``spark`` is auto-injected only into the top-level notebook namespace. Functions
# defined in this %run-ed module resolve globals against THIS module, not the caller,
# so bind the active session here (fixes NameError: name 'spark' is not defined).
spark = SparkSession.getActiveSession()

_REGISTRY_SEED_BATCH = 50
_DC_EXTRACT_ZIP_NAME = 'autodesk_data_extract.zip'

_PK_CONFIG_UPDATABLE_SOURCES = frozenset({'auto_position_1', 'explicit_pk_config'})
_PK_CONFIG_MERGE_UPDATE_CONDITION = (
    "target.source IN ('auto_position_1', 'explicit_pk_config') AND "
    "(target.pk_columns != source.pk_columns OR target.source != source.source)"
)

# Module globals — populated by init_config(); refreshed in prepare_shared_bootstrap().
CATALOG = ''
SCHEMA_BRONZE = 'bronze'
VOLUME_BASE = ''
VOLUME_LAYOUT = 'legacy'
TEST_MODE = False
DC_SNAPSHOT_PATH = ''
DC_CDC_PATH = ''
DC_PROJECT_ID = ''
SNAPSHOT_GROUP_FILTER = None  # frozenset | None — None means all snapshot-only groups
CDC_GROUP_FILTER = None       # frozenset | None — None means all CDC groups
DC_BASE = ''
DC_CDC_BASE = ''
RAW_SNAPSHOTS_BASE = ''
RAW_DELTAS_BASE = ''
META_REGISTRY_TABLE = '_meta_bronze_pk_registry'
META_STATUS_TABLE = '_meta_bronze_table_status'
META_SCHEMA_VERSIONS_TABLE = '_meta_bronze_schema_versions'
CANONICAL_SCHEMA_PATH = ''
FQN_REGISTRY = ''
FQN_STATUS = ''
FQN_SCHEMA_VERSIONS = ''
_TABLE_REGISTRY = ''
_TABLE_STATUS = ''
_TABLE_SCHEMA_VERSIONS = ''
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
RUN_ID = 'unknown'
RUN_STARTED_AT = 0.0
STATUS_OK = 'ok'
STATUS_PK_MISSING_IN_CSV = 'pk_missing_in_csv'
STATUS_PK_NOT_UNIQUE = 'pk_not_unique'
STATUS_PK_NULL_VALUES = 'pk_null_values'
STATUS_CSV_UNREADABLE = 'csv_unreadable'
STATUS_TYPE_INCOMPAT = 'type_incompat'
STATUS_SKIPPED_NO_CSV = 'skipped_no_csv'
STATUS_NOT_IN_SCHEMA_JSON = 'not_in_schema_json'
PK_AUDIT_BLOCK_STATUSES = frozenset({
    STATUS_PK_MISSING_IN_CSV,
    STATUS_PK_NULL_VALUES,
})
_pk_audit_mod = None
_run_counts: dict[str, int] = {}


def _parse_service_group_filter(raw: str) -> frozenset | None:
    """Parse comma-separated ACC service group names; empty means no filter."""
    text = (raw or '').strip()
    if not text:
        return None
    groups = frozenset(g.strip() for g in text.split(',') if g.strip())
    return groups or None


def snapshot_service_group_allowed(schema_name: str) -> bool:
    """Return False when acc.snapshot_service_groups excludes this schema."""
    if SNAPSHOT_GROUP_FILTER is None:
        return True
    return schema_name in SNAPSHOT_GROUP_FILTER


def cdc_service_group_allowed(group_name: str) -> bool:
    """Return False when acc.cdc_service_groups excludes this CDC group."""
    if CDC_GROUP_FILTER is None:
        return True
    return group_name in CDC_GROUP_FILTER


def init_config() -> None:
    """Read spark.conf and set module-level pipeline globals."""
    global CATALOG, SCHEMA_BRONZE, VOLUME_BASE, VOLUME_LAYOUT, TEST_MODE
    global DC_SNAPSHOT_PATH, DC_CDC_PATH, DC_PROJECT_ID
    global SNAPSHOT_GROUP_FILTER, CDC_GROUP_FILTER
    global DC_BASE, DC_CDC_BASE, RAW_SNAPSHOTS_BASE, RAW_DELTAS_BASE
    global META_REGISTRY_TABLE, META_STATUS_TABLE, META_SCHEMA_VERSIONS_TABLE
    global CANONICAL_SCHEMA_PATH, FQN_REGISTRY, FQN_STATUS, FQN_SCHEMA_VERSIONS
    global _TABLE_REGISTRY, _TABLE_STATUS, _TABLE_SCHEMA_VERSIONS
    global RUN_ID, RUN_STARTED_AT
    global STATUS_OK, STATUS_PK_MISSING_IN_CSV, STATUS_PK_NOT_UNIQUE
    global STATUS_PK_NULL_VALUES, STATUS_CSV_UNREADABLE
    global STATUS_TYPE_INCOMPAT, STATUS_SKIPPED_NO_CSV, STATUS_NOT_IN_SCHEMA_JSON
    global PK_AUDIT_BLOCK_STATUSES, _run_counts

    CATALOG = spark.conf.get('acc.catalog', 'final_poc')
    SCHEMA_BRONZE = 'bronze'
    VOLUME_BASE = f'/Volumes/{CATALOG}/{SCHEMA_BRONZE}/acc_bronze_volume'
    VOLUME_LAYOUT = spark.conf.get('acc.volume_layout', 'legacy').strip().lower()
    TEST_MODE = spark.conf.get('acc.test_mode', 'false').strip().lower() in (
        'true', '1', 'yes',
    )
    DC_SNAPSHOT_PATH = spark.conf.get('acc.dc_snapshot_path', '').strip()
    DC_CDC_PATH = spark.conf.get('acc.dc_cdc_path', '').strip()
    DC_PROJECT_ID = spark.conf.get('acc.dc_project_id', '').strip()
    SNAPSHOT_GROUP_FILTER = _parse_service_group_filter(
        spark.conf.get('acc.snapshot_service_groups', ''),
    )
    CDC_GROUP_FILTER = _parse_service_group_filter(
        spark.conf.get('acc.cdc_service_groups', ''),
    )
    DC_BASE = f'{VOLUME_BASE}/data_connector'
    DC_CDC_BASE = f'{VOLUME_BASE}/data_connector_cdc'
    RAW_SNAPSHOTS_BASE = f'{VOLUME_BASE}/raw/snapshots'
    RAW_DELTAS_BASE = f'{VOLUME_BASE}/raw/deltas'

    META_REGISTRY_TABLE = '_meta_bronze_pk_registry'
    META_STATUS_TABLE = '_meta_bronze_table_status'
    META_SCHEMA_VERSIONS_TABLE = '_meta_bronze_schema_versions'
    CANONICAL_SCHEMA_PATH = f'{VOLUME_BASE}/schema.json'
    FQN_REGISTRY = f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_REGISTRY_TABLE}`'
    FQN_STATUS = f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_STATUS_TABLE}`'
    FQN_SCHEMA_VERSIONS = (
        f'`{CATALOG}`.`{SCHEMA_BRONZE}`.`{META_SCHEMA_VERSIONS_TABLE}`'
    )
    _TABLE_REGISTRY = f'{CATALOG}.{SCHEMA_BRONZE}.{META_REGISTRY_TABLE}'
    _TABLE_STATUS = f'{CATALOG}.{SCHEMA_BRONZE}.{META_STATUS_TABLE}'
    _TABLE_SCHEMA_VERSIONS = (
        f'{CATALOG}.{SCHEMA_BRONZE}.{META_SCHEMA_VERSIONS_TABLE}'
    )

    RUN_ID = spark.conf.get('spark.databricks.clusterUsageTags.runId', 'unknown')
    RUN_STARTED_AT = time.time()

    STATUS_OK = 'ok'
    STATUS_PK_MISSING_IN_CSV = 'pk_missing_in_csv'
    STATUS_PK_NOT_UNIQUE = 'pk_not_unique'
    STATUS_PK_NULL_VALUES = 'pk_null_values'
    STATUS_CSV_UNREADABLE = 'csv_unreadable'
    STATUS_TYPE_INCOMPAT = 'type_incompat'
    STATUS_SKIPPED_NO_CSV = 'skipped_no_csv'
    STATUS_NOT_IN_SCHEMA_JSON = 'not_in_schema_json'
    PK_AUDIT_BLOCK_STATUSES = frozenset({
        STATUS_PK_MISSING_IN_CSV,
        STATUS_PK_NULL_VALUES,
    })
    _run_counts = {
        STATUS_OK:                 0,
        STATUS_PK_MISSING_IN_CSV:  0,
        STATUS_PK_NOT_UNIQUE:      0,
        STATUS_PK_NULL_VALUES:     0,
        STATUS_CSV_UNREADABLE:     0,
        STATUS_TYPE_INCOMPAT:      0,
        STATUS_SKIPPED_NO_CSV:     0,
        STATUS_NOT_IN_SCHEMA_JSON: 0,
        'schema_changes':          0,
        'newly_discovered':        0,
    }


def _emit(level: str, message: str) -> None:
    print(f'[{level}] {message}')


def _sql_str(s: str) -> str:
    """Sanitize a STRING literal for inline SQL."""
    return s.replace("'", "''")


def build_target_schema(table_def: dict) -> StructType:
    """Maps schema.json data_type to PySpark types."""
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
                       if isinstance(kv[1], dict) else 9999,
    )

    fields = []
    for col_name, col_meta in sorted_cols:
        if not isinstance(col_meta, dict):
            continue
        data_type_str = col_meta.get('data_type', 'string').lower()
        spark_type = type_map.get(data_type_str, StringType())
        fields.append(StructField(col_name, spark_type, nullable=True))

    return StructType(fields)


def _schema_for_bronze_table(evolved_struct: StructType | None) -> StructType | None:
    """Declared schema for SCD Type 1 bronze streaming tables (no history columns)."""
    return evolved_struct


def _pk_audit_module():
    """Lazy-load pk_audit_logic from the same shared/ directory."""
    global _pk_audit_mod
    if _pk_audit_mod is not None:
        return _pk_audit_mod
    import importlib.util
    audit_path = Path(__file__).resolve().parent / 'pk_audit_logic.py'
    spec = importlib.util.spec_from_file_location('pk_audit_logic', audit_path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load pk_audit_logic from {audit_path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _pk_audit_mod = mod
    return mod


def audit_table_pk(
    csv_path: str,
    schema_name: str,
    table_name: str,
    csv_stem: str,
    pk_columns: list[str],
    pk_source: str,
    header: list[str] | None = None,
    prefer_run_path: str | None = None,
) -> tuple[str, str, list[str]]:
    """Run PK audit on one CSV. Returns (status, error_message, header)."""
    return _pk_audit_module().audit_pk_csv(
        spark, csv_path, schema_name, table_name, csv_stem,
        pk_columns, pk_source, header=header, prefer_run_path=prefer_run_path,
    )


def _ensure_run_counts() -> None:
    """Initialize missing counter keys (DLT may skip module-level init_config)."""
    defaults = {
        STATUS_OK:                 0,
        STATUS_PK_MISSING_IN_CSV:  0,
        STATUS_PK_NOT_UNIQUE:      0,
        STATUS_PK_NULL_VALUES:     0,
        STATUS_CSV_UNREADABLE:     0,
        STATUS_TYPE_INCOMPAT:      0,
        STATUS_SKIPPED_NO_CSV:     0,
        STATUS_NOT_IN_SCHEMA_JSON: 0,
        'schema_changes':          0,
        'newly_discovered':        0,
    }
    for key, val in defaults.items():
        _run_counts.setdefault(key, val)


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
    """Build a reconciled schema merging target types with schema.json changes."""
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
    if TEST_MODE or not rows:
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
    config_type: str, config_hash: str, config_path: str,
) -> None:
    """Track pk_config or schema_json hash in _meta_bronze_schema_versions."""
    if TEST_MODE:
        return
    project_id = DC_PROJECT_ID or 'unknown'
    source_df = (
        spark.createDataFrame(
            [(CATALOG, project_id, config_type, config_hash, config_path)],
            ['catalog', 'project_id', 'config_type', 'config_hash', 'config_path'],
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
            'target.config_type = source.config_type',
        )
        .whenMatchedUpdate(set={
            'config_hash': 'source.config_hash',
            'last_applied_at': 'source.last_applied_at',
            'config_path': 'source.config_path',
        })
        .whenNotMatchedInsertAll()
        .execute()
    )


def _seed_pk_registry_from_config() -> None:
    """Sync registry from pk_config.json when file hash changes (upsert).

    Best-effort inside SDP — the ``seed_registry`` job task runs the same MERGE
    outside the pipeline so rows persist before snapshot_pipeline starts.
    """
    if TEST_MODE:
        _emit('INFO', 'TEST_MODE — skipping PK registry sync from pk_config.json')
        return

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
            f"WHERE catalog = '{_sql_str(CATALOG)}'",
        ).collect()[0]['n'])
    except Exception:
        pass

    stored_hash = None
    try:
        prev = spark.sql(
            f'SELECT config_hash FROM {FQN_SCHEMA_VERSIONS} '
            f"WHERE catalog = '{_sql_str(CATALOG)}' "
            f"  AND config_type = 'pk_config'",
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
            f"WHERE catalog = '{_sql_str(CATALOG)}'",
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
                [
                    (CATALOG, s, t, pk_cols, 'explicit_pk_config')
                    for s, t, pk_cols in batch
                ],
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


def _load_registry() -> tuple[dict, set]:
    """Load _meta_bronze_pk_registry into flat_lookup and known_schemas."""
    flat: dict[str, dict] = {}
    schemas: set[str] = set()
    try:
        df = spark.sql(
            f'SELECT `schema`, `table`, pk_columns, source FROM {FQN_REGISTRY}',
        )
        for row in df.collect():
            sch = row['schema']
            tbl = row['table']
            pks = list(row['pk_columns'] or [])
            flat[f'{sch}_{tbl}'.lower()] = {
                'schema':     sch,
                'table':      tbl,
                'pk_columns': pks,
                'source':     row['source'] or 'unknown',
            }
            schemas.add(sch)
    except Exception as exc:
        _emit('WARN', f'registry read failed: {exc}; treating as empty')
    return flat, schemas


def _split_csv_stem(stem: str, known_schemas: set) -> tuple[str, str]:
    """Best-effort split of a CSV stem into (schema, table)."""
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
    """Return the column names from CSVs matching glob_path."""
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


def _record_status(
    schema_name: str,
    table_name: str,
    status: str,
    csv_header: list,
    error_msg: str | None = None,
    column_diff: dict | None = None,
) -> None:
    """MERGE one row into _meta_bronze_table_status."""
    if TEST_MODE:
        return
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
    """Insert a brand-new (catalog, schema, table) row into the PK registry."""
    try:
        _delta_merge_registry_rows([(schema_name, table_name, pk_col)])
        _run_counts['newly_discovered'] += 1
    except Exception as exc:
        _emit('WARN', f'registry INSERT failed for {schema_name}.{table_name}: {exc}')


def _table_name_from_csv(filename: str) -> str:
    """Convert ``admin_users.csv`` → ``admin_users`` (lowercased, sanitized)."""
    stem = filename[:-4] if filename.lower().endswith('.csv') else filename
    return re.sub(r'[^a-z0-9_]', '_', stem.lower())


def _resolve_latest_run_base(dc_root: str, label: str) -> str:
    """Pick the lexicographically latest run directory under dc_root."""
    try:
        if DC_PROJECT_ID:
            project_dirs = [f'{dc_root}/{DC_PROJECT_ID}']
        else:
            project_dirs = [e.path for e in _fs_ls(dc_root) if e.isDir()]
    except Exception:
        return ''

    latest_path = ''
    latest_key = ''
    for proj_dir in project_dirs:
        try:
            run_dirs = [e.path for e in _fs_ls(proj_dir) if e.isDir()]
        except Exception:
            continue
        for run_dir in run_dirs:
            name = run_dir.rstrip('/').split('/')[-1]
            if name > latest_key:
                latest_key = name
                latest_path = run_dir.rstrip('/')
    if latest_path:
        _emit('INFO', f'{label} base (latest run): {latest_path}')
    return latest_path


def _resolve_snapshot_base() -> str:
    """Directory containing this sync's DC CSV snapshot."""
    if DC_SNAPSHOT_PATH:
        return DC_SNAPSHOT_PATH.rstrip('/')

    legacy_path = _resolve_latest_run_base(DC_BASE, 'snapshot')
    if legacy_path:
        return legacy_path

    if VOLUME_LAYOUT == 'v2' and _fs_exists(RAW_SNAPSHOTS_BASE):
        _emit('INFO', f'snapshot base (v2): {RAW_SNAPSHOTS_BASE}')
        return RAW_SNAPSHOTS_BASE

    return ''


def _resolve_cdc_base() -> str:
    """Directory containing this sync's CDC CSV drop."""
    if DC_CDC_PATH:
        return DC_CDC_PATH.rstrip('/')

    legacy_path = _resolve_latest_run_base(DC_CDC_BASE, 'CDC')
    if legacy_path:
        return legacy_path

    if VOLUME_LAYOUT == 'v2' and _fs_exists(RAW_DELTAS_BASE):
        _emit('INFO', f'CDC base (v2): {RAW_DELTAS_BASE}')
        return RAW_DELTAS_BASE

    return ''


def resolve_project_cdc_root(cdc_run_path: str = '') -> str:
    """Stable CDC stream root: ``.../data_connector_cdc/{project_id}/``.

    ``acc.dc_cdc_path`` points at one timestamp run folder; the stream reads
    from the parent so checkpoints stay stable across daily syncs.
    """
    run_path = (cdc_run_path or DC_CDC_PATH or _resolve_cdc_base()).rstrip('/')
    if not run_path:
        return ''
    return os.path.dirname(run_path)


def resolve_cdc_csv_glob(project_cdc_root: str, csv_name: str) -> str:
    """Glob for one CDC CSV across all timestamp subfolders under a project."""
    return f'{project_cdc_root.rstrip("/")}/*/{csv_name}'


def discover_cdc_csv_basenames(project_cdc_root: str) -> list[str]:
    """Unique CDC CSV basenames across all timestamp folders for one project."""
    if not project_cdc_root:
        return []
    seen: set[str] = set()
    try:
        for name in os.listdir(project_cdc_root):
            sub = os.path.join(project_cdc_root, name)
            if not os.path.isdir(sub):
                continue
            for fn in os.listdir(sub):
                if fn.lower().endswith('.csv'):
                    seen.add(fn)
    except Exception as exc:
        _emit('WARN', f'cannot list CDC CSVs under {project_cdc_root}: {exc}')
        return []
    return sorted(seen)


def cdc_csv_exists_in_project(project_cdc_root: str, csv_name: str) -> bool:
    """Return True when ``csv_name`` exists in any timestamp subfolder."""
    if not project_cdc_root or not csv_name:
        return False
    try:
        for name in os.listdir(project_cdc_root):
            sub = os.path.join(project_cdc_root, name)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, csv_name)):
                return True
    except Exception:
        return False
    return False


def _discover_csv_basenames(snapshot_base: str) -> list[str]:
    """Return CSV file names in ``snapshot_base`` (Spark Connect / SDP safe)."""
    if not snapshot_base:
        return []
    try:
        names = sorted(
            name for name in os.listdir(snapshot_base)
            if name.lower().endswith('.csv')
        )
    except Exception as exc:
        _emit('WARN', f'cannot list CSVs under {snapshot_base}: {exc}')
        return []
    if not names:
        if _fs_exists(snapshot_base):
            _emit('WARN', f'no CSV files found under {snapshot_base}')
        else:
            _emit('WARN', f'snapshot base not found: {snapshot_base}')
    return names


def discover_all_csv_basenames(base: str) -> list[tuple[str, str]]:
    """Recursively discover CSVs under v2 raw/snapshots layout (SDP safe).

    Returns sorted list of (service_group, csv_filename) tuples.
    """
    if not base:
        return []

    discovered: list[tuple[str, str]] = []

    def _walk(current: str, service_group: str | None) -> None:
        try:
            entries = os.listdir(current)
        except Exception:
            return
        for name in entries:
            full_path = os.path.join(current, name)
            if os.path.isdir(full_path):
                group = service_group or name
                _walk(full_path, group)
            elif name.lower().endswith('.csv') and service_group:
                discovered.append((service_group, name))

    _walk(base.rstrip('/'), None)
    return sorted(discovered)


def resolve_snapshot_csv_path(service_group: str, csv_name: str) -> str:
    """Absolute CSV path for v2 snapshot layout."""
    return f'{RAW_SNAPSHOTS_BASE}/{service_group}/{csv_name}'


def resolve_delta_glob(service_group: str, csv_name: str) -> str:
    """Glob path for v2 delta CSV drops (date-partitioned folders)."""
    return f'{RAW_DELTAS_BASE}/{service_group}/*/{csv_name}'


def _resolve_schema_table(
    csv_name: str, table_name: str, registry: dict, known_schemas: set,
) -> tuple[str, str]:
    """Best-effort (schema, table) resolution for log/status writes."""
    flat_key = table_name.lower()
    if flat_key in registry:
        return registry[flat_key]['schema'], registry[flat_key]['table']
    return _split_csv_stem(table_name, known_schemas)


def read_csv_df(
    glob_path: str,
    evolved_struct: StructType | None,
    filter_deleted_at: bool = False,
):
    """Read CSV with evolved schema (or string fallback) and optional tombstone filter."""
    if evolved_struct is not None:
        df = (spark.read.format('csv')
              .schema(evolved_struct)
              .option('header', True)
              .option('multiLine', True)
              .option('mode', 'PERMISSIVE')
              .option('nullValue', '')
              .option('emptyValue', '')
              .csv(glob_path))
    else:
        df = (spark.read.format('csv')
              .option('header', True)
              .option('multiLine', True)
              .option('inferSchema', False)
              .csv(glob_path))
    if filter_deleted_at and 'deleted_at' in df.columns:
        df = df.filter(F.col('deleted_at').isNull())
    return df


def gate_table_common(
    csv_name: str,
    registry: dict,
    known_schemas: set,
    glob_path: str,
    evolved_schemas: dict,
    schema_changed: bool,
) -> dict | None:
    """Shared PK/header gating for snapshot and CDC pipelines.

    Returns dict with sch, tbl, pk_columns, header, column_diff, table_name,
    evolved_struct on success; None when the table should be skipped.
    """
    table_name = _table_name_from_csv(csv_name)

    try:
        header = _read_csv_header(glob_path)
    except FileNotFoundError:
        sch, tbl = _resolve_schema_table(csv_name, table_name, registry, known_schemas)
        _emit('WARN', f'{sch}.{tbl}: skipped — no CSV file in volume for this table this run')
        _record_status(sch, tbl, STATUS_SKIPPED_NO_CSV, csv_header=[],
                       error_msg='no matching CSV file at glob')
        _run_counts[STATUS_SKIPPED_NO_CSV] += 1
        return None
    except Exception as exc:
        sch, tbl = _resolve_schema_table(csv_name, table_name, registry, known_schemas)
        _emit('ERROR', f'{sch}.{tbl}: skipped — CSV unreadable or empty ({exc})')
        _record_status(sch, tbl, STATUS_CSV_UNREADABLE, csv_header=[],
                       error_msg=str(exc))
        _run_counts[STATUS_CSV_UNREADABLE] += 1
        return None

    flat_key = table_name.lower()
    pk_source = 'auto_position_1'
    if flat_key in registry:
        sch = registry[flat_key]['schema']
        tbl = registry[flat_key]['table']
        pk_columns = registry[flat_key]['pk_columns']
        pk_source = registry[flat_key].get('source', 'unknown')
    else:
        sch, tbl = _split_csv_stem(table_name, known_schemas)
        if sch == 'unknown':
            _emit('WARN', f'unknown.{tbl}: skipped — no registry entry and stem does not match any known schema')
            _record_status(sch, tbl, STATUS_NOT_IN_SCHEMA_JSON, csv_header=header,
                           error_msg='registry has no row; stem does not match any known schema')
            _run_counts[STATUS_NOT_IN_SCHEMA_JSON] += 1
            return None
        pk_col = header[0] if header else None
        if not pk_col:
            _emit('ERROR', f'{sch}.{tbl}: skipped — empty CSV header')
            _record_status(sch, tbl, STATUS_CSV_UNREADABLE, csv_header=header,
                           error_msg='empty header')
            _run_counts[STATUS_CSV_UNREADABLE] += 1
            return None
        _insert_registry_row(sch, tbl, pk_col)
        pk_columns = [pk_col]
        pk_source = 'auto_position_1'
        _emit('INFO', f'new table {sch}.{tbl} discovered; PK locked as {pk_columns} (source=auto_position_1)')

    header_set = set(header)
    missing = [c for c in pk_columns if c not in header_set]
    if missing:
        _emit('ERROR',
              f'{sch}.{tbl}: skipped — required PK column(s) {missing} '
              f'missing from CSV header. Actual header: {header}')
        _record_status(sch, tbl, STATUS_PK_MISSING_IN_CSV, csv_header=header,
                       error_msg=f'PK column(s) {missing} missing from CSV header')
        _run_counts[STATUS_PK_MISSING_IN_CSV] += 1
        return None

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

    return {
        'sch': sch,
        'tbl': tbl,
        'pk_columns': pk_columns,
        'pk_source': pk_source,
        'header': header,
        'column_diff': column_diff,
        'table_name': table_name,
        'evolved_struct': evolved_struct,
    }


def _sweep_heartbeat(processed_keys: set) -> None:
    """Bump last_synced_at on registry rows not processed this run."""
    if TEST_MODE:
        return
    try:
        registry_df = spark.table(_TABLE_REGISTRY).filter(
            F.col('catalog') == CATALOG,
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
                'registry entry exists but no CSV in this run',
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


class _FsEntry:
    """Minimal dbutils.fs.ls entry shape for os.listdir-based volume listing."""

    __slots__ = ('path', 'name', '_is_dir')

    def __init__(self, path: str, name: str, is_dir: bool) -> None:
        self.path = path
        self.name = name
        self._is_dir = is_dir

    def isDir(self) -> bool:
        return self._is_dir


def _fs_exists(path: str) -> bool:
    """Check path existence using native Python (Spark Connect / SDP safe)."""
    if not path:
        return False
    try:
        return os.path.exists(path)
    except Exception:
        return False


def _fs_ls(path: str) -> list[_FsEntry]:
    """List a directory via os.listdir (/Volumes is mounted locally in SDP)."""
    if not path:
        return []
    try:
        if not os.path.isdir(path):
            return []
        base = path.rstrip('/')
        return [
            _FsEntry(
                os.path.join(base, name),
                name,
                os.path.isdir(os.path.join(base, name)),
            )
            for name in os.listdir(path)
        ]
    except Exception as exc:
        _emit('WARN', f'_fs_ls failed for {path}: {exc}')
        return []


def _read_volume_bytes(path: str) -> bytes:
    """Read file bytes using native Python (Spark Connect / SDP safe)."""
    with open(path, 'rb') as handle:
        return handle.read()


def _write_volume_json(path: str, doc: dict) -> None:
    """Write JSON to a volume path using native Python (Spark Connect / SDP safe)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(doc, handle, indent=2)


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
    entries = _fs_ls(schemas_dir)
    if not entries:
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


def _bootstrap_schema_json(resolve_base_fn=_resolve_snapshot_base) -> str | None:
    """Resolve the canonical schema.json, preferring the connector-published file.

    The connector publishes schema.json to the volume on every sync, applying
    the APS Schema API first / export ZIP fallback rule in one place. An
    existing file is therefore the current authoritative source, so it wins.
    The ZIP paths below are the fallback for when nothing was ever published
    (first run before any sync, or a failed upload).
    """
    if _fs_exists(CANONICAL_SCHEMA_PATH):
        _emit('INFO', f'Using published {CANONICAL_SCHEMA_PATH} (Schema API primary)')
        return CANONICAL_SCHEMA_PATH

    snapshot_base = resolve_base_fn()

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
        # Reached only when nothing was published, so there is no existing file
        # to hash against — seed it from the ZIP for this run and the next.
        if not TEST_MODE:
            _write_volume_json(CANONICAL_SCHEMA_PATH, fresh_doc)
            _emit('INFO', f'schema.json seeded at {CANONICAL_SCHEMA_PATH} from zip {used_zip}')
        else:
            _emit('INFO', f'TEST_MODE — schema.json would be seeded from zip {used_zip}')
        return CANONICAL_SCHEMA_PATH

    schemas_dir = f'{snapshot_base}/schemas' if snapshot_base else ''
    if schemas_dir and _fs_exists(schemas_dir):
        merged = _merge_per_domain_schemas(schemas_dir)
        if merged:
            if not TEST_MODE:
                _write_volume_json(CANONICAL_SCHEMA_PATH, merged)
                _emit('INFO', f'merged per-domain schemas into {CANONICAL_SCHEMA_PATH}')
            else:
                _emit('INFO', 'TEST_MODE — would merge per-domain schemas into schema.json')
            return CANONICAL_SCHEMA_PATH

    _emit('WARN', 'canonical schema.json not found — registry/forma validation skipped')
    return None


def _load_typed_schemas(schema_path: str | None) -> tuple[dict, bool]:
    """Build typed StructType per table from schema.json (hash-gated)."""
    if not schema_path or not _fs_exists(schema_path):
        return {}, False

    schema_bytes = _read_volume_bytes(schema_path)
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()

    schema_changed = False
    try:
        prev = spark.sql(
            f"SELECT config_hash FROM {FQN_SCHEMA_VERSIONS} "
            f"WHERE catalog = '{_sql_str(CATALOG)}' "
            f"  AND config_type = 'schema_json'",
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


def load_cdc_typed_schemas_from_volume() -> dict:
    """Load CDC per-domain schema JSON files from snapshot/CDC schemas directories.

    Returns {cdcissues_issues: StructType, ...} keyed by schema_table stem.
    """
    schemas: dict[str, StructType] = {}
    schema_dirs: list[str] = []

    snapshot_base = _resolve_snapshot_base()
    cdc_base = _resolve_cdc_base()
    for base in (snapshot_base, cdc_base):
        if base:
            schema_dirs.append(f'{base}/schemas')

    if VOLUME_LAYOUT == 'v2':
        schema_dirs.extend([
            f'{RAW_SNAPSHOTS_BASE}/schemas',
            f'{RAW_DELTAS_BASE}/schemas',
        ])

    seen_dirs: set[str] = set()
    for schemas_dir in schema_dirs:
        if schemas_dir in seen_dirs or not _fs_exists(schemas_dir):
            continue
        seen_dirs.add(schemas_dir)
        entries = _fs_ls(schemas_dir)
        if not entries:
            continue
        for entry in entries:
            name_lower = entry.name.lower()
            if entry.isDir() or not name_lower.startswith('cdc'):
                continue
            domain = name_lower[:-5] if name_lower.endswith('.json') else name_lower
            if not cdc_service_group_allowed(domain):
                continue
            if not name_lower.endswith('.json') or name_lower == 'schema.json':
                continue
            domain = entry.name[:-5]
            try:
                doc = json.loads(_read_volume_bytes(entry.path).decode('utf-8'))
            except Exception as exc:
                _emit('WARN', f'cannot read CDC schema {entry.path}: {exc}')
                continue
            if not _is_two_level_schema_doc(doc):
                continue
            for tbl_name, columns in doc.items():
                if not isinstance(columns, dict):
                    continue
                try:
                    key = f'{domain}_{tbl_name}'.lower()
                    schemas[key] = build_target_schema(columns)
                except Exception as e:
                    _emit('WARN', f'Failed to build CDC schema for {domain}.{tbl_name}: {e}')

    return schemas


def seed_cdc_pk_registry_from_schemas(cdc_typed_schemas: dict) -> None:
    """Seed PK registry from first column of each CDC typed schema."""
    if TEST_MODE:
        _emit('INFO', 'TEST_MODE — skipping CDC PK registry seed')
        return
    if not cdc_typed_schemas:
        return

    registry, known_schemas = _load_registry()
    extra_schemas = {key.split('_')[0] for key in cdc_typed_schemas if '_' in key}
    known_schemas = known_schemas | extra_schemas

    rows: list[tuple[str, str, str]] = []
    for key, struct in cdc_typed_schemas.items():
        if not struct.fields:
            continue
        if key.lower() in registry:
            continue
        sch, tbl = _split_csv_stem(key, known_schemas)
        if sch == 'unknown':
            continue
        pk_col = struct.fields[0].name
        rows.append((sch, tbl, pk_col))

    if not rows:
        _emit('INFO', 'CDC PK registry seed — no new tables to insert')
        return

    for i in range(0, len(rows), _REGISTRY_SEED_BATCH):
        batch = rows[i:i + _REGISTRY_SEED_BATCH]
        _delta_merge_registry_rows(batch)

    _emit('INFO', f'CDC PK registry seeded ({len(rows)} tables from typed schemas)')


def prepare_shared_bootstrap(
    seed_pk: bool = True,
    bootstrap_schema: bool = True,
    resolve_base_fn=_resolve_snapshot_base,
) -> tuple:
    """Run shared Phase-1 bootstrap: PK sync, schema load, registry, evolution.

    Re-reads ``spark.conf`` here because Lakeflow pipeline configuration is not
    always available when this module is first imported via ``%run``.

    Returns:
        (registry, known_schemas, typed_schemas, schema_changed,
         evolved_schemas, base_path)
    """
    init_config()
    snap_filter = (
        ','.join(sorted(SNAPSHOT_GROUP_FILTER))
        if SNAPSHOT_GROUP_FILTER is not None else '(all)'
    )
    cdc_filter = (
        ','.join(sorted(CDC_GROUP_FILTER))
        if CDC_GROUP_FILTER is not None else '(all)'
    )
    _emit(
        'INFO',
        f'pipeline config: catalog={CATALOG} project_id={DC_PROJECT_ID or "(unset)"} '
        f'snapshot_path={DC_SNAPSHOT_PATH or "(unset)"} cdc_path={DC_CDC_PATH or "(unset)"} '
        f'layout={VOLUME_LAYOUT} snapshot_groups={snap_filter} cdc_groups={cdc_filter}',
    )

    _ensure_run_counts()
    if seed_pk:
        _seed_pk_registry_from_config()

    if not TEST_MODE:
        _apply_table_documentation()

    typed_schemas: dict = {}
    schema_changed = False
    schema_path = None
    if bootstrap_schema:
        schema_path = _bootstrap_schema_json(resolve_base_fn=resolve_base_fn)
        typed_schemas, schema_changed = _load_typed_schemas(schema_path)

    registry, known_schemas = _load_registry()
    if schema_path and _fs_exists(schema_path):
        try:
            schema_doc = json.loads(_read_volume_bytes(schema_path).decode('utf-8'))
            known_schemas = known_schemas | {
                k for k, v in schema_doc.items() if isinstance(v, dict)
            }
        except Exception as exc:
            _emit('WARN', f'could not enrich known_schemas from schema.json: {exc}')
    base_path = resolve_base_fn()
    if base_path:
        _emit('INFO', f'resolved data base path: {base_path}')
    else:
        _emit(
            'WARN',
            'resolved data base path is empty — no CSVs will be registered this run',
        )
    evolved_schemas = _build_all_evolved_schemas(
        typed_schemas, schema_changed, known_schemas,
    )
    return (
        registry, known_schemas, typed_schemas, schema_changed,
        evolved_schemas, base_path,
    )


def _apply_table_documentation() -> None:
    """Add Unity Catalog comments to meta tables for discoverability."""
    if TEST_MODE:
        return
    try:
        spark.sql(
            f"COMMENT ON TABLE {FQN_REGISTRY} IS "
            "'Primary key registry for ACC Bronze tables. Seeded from pk_config.json. "
            "Used by AUTO CDC FROM SNAPSHOT to identify rows across snapshots. "
            "Source: explicit_pk_config (from pk_config.json) or auto_position_1 (legacy).'",
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
            "Statuses: ok, pk_missing_in_csv, csv_unreadable, type_incompat, skipped_no_csv, not_in_schema_json.'",
        )
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN last_run_status COMMENT 'Outcome of most recent sync: ok | pk_missing_in_csv | csv_unreadable | type_incompat'")
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN column_diff COMMENT 'Schema drift details: {{added: [...], absent_in_csv: [...], type_widened: [...]}}'")
        spark.sql(f"ALTER TABLE {FQN_STATUS} ALTER COLUMN last_synced_at COMMENT 'Heartbeat — updated on every sync run regardless of status'")

        spark.sql(
            f"COMMENT ON TABLE {FQN_SCHEMA_VERSIONS} IS "
            "'Tracks schema.json and pk_config.json hashes per project. "
            "Used for change detection — pipeline skips re-processing if hash unchanged.'",
        )
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_type COMMENT 'Type of config: schema_json | pk_config'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_hash COMMENT 'SHA-256 hash of config file content — used for change detection'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN config_path COMMENT 'Volume path to the config file'")
        spark.sql(f"ALTER TABLE {FQN_SCHEMA_VERSIONS} ALTER COLUMN last_applied_at COMMENT 'When this config was last processed/applied'")

        _emit('INFO', 'Table documentation (comments) applied to meta tables')
    except Exception as e:
        _emit('WARN', f'Table documentation skipped — {e}')


def emit_summary() -> None:
    """Emit the end-of-run [SUMMARY] line with aggregated counters."""
    _ensure_run_counts()
    duration_s = int(time.time() - RUN_STARTED_AT)
    hh, rem = divmod(duration_s, 3600)
    mm, ss = divmod(rem, 60)
    _emit('SUMMARY',
          f'sync_run_id={RUN_ID} duration={hh:02d}:{mm:02d}:{ss:02d}\n'
          f'  ingested_clean: {_run_counts[STATUS_OK]}\n'
          f'  schema_changes: {_run_counts["schema_changes"]}\n'
          f'  skipped_pk_missing: {_run_counts[STATUS_PK_MISSING_IN_CSV]}\n'
          f'  skipped_pk_not_unique: {_run_counts[STATUS_PK_NOT_UNIQUE]}\n'
          f'  skipped_pk_null_values: {_run_counts[STATUS_PK_NULL_VALUES]}\n'
          f'  skipped_type_incompat: {_run_counts[STATUS_TYPE_INCOMPAT]}\n'
          f'  skipped_csv_unreadable: {_run_counts[STATUS_CSV_UNREADABLE]}\n'
          f'  skipped_no_csv: {_run_counts[STATUS_SKIPPED_NO_CSV]}\n'
          f'  skipped_not_in_schema_json: {_run_counts[STATUS_NOT_IN_SCHEMA_JSON]}\n'
          f'  newly_discovered: {_run_counts["newly_discovered"]}')


init_config()
