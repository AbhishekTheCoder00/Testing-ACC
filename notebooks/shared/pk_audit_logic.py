# Databricks notebook source
# PK audit — pure helpers + Spark CSV checks (no DLT).
# Used by seed_registry (%run) and acc_pipeline_common (same directory import).

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Status constants (keep in sync with acc_pipeline_common.py)
# ---------------------------------------------------------------------------
STATUS_OK = 'ok'
STATUS_PK_MISSING_IN_CSV = 'pk_missing_in_csv'
STATUS_PK_NOT_UNIQUE = 'pk_not_unique'
STATUS_PK_NULL_VALUES = 'pk_null_values'
STATUS_CSV_UNREADABLE = 'csv_unreadable'

# Only hard failures block CDC registration. pk_not_unique is recorded as a
# warning — CDC CSVs often contain multiple row versions per PK (sequence_by
# resolves them in create_auto_cdc_flow).
PK_AUDIT_BLOCK_STATUSES = frozenset({
    STATUS_PK_MISSING_IN_CSV,
    STATUS_PK_NULL_VALUES,
})


def format_pk_audit_message(
    schema_name: str,
    table_name: str,
    csv_stem: str,
    status: str,
    pk_columns: list[str],
    *,
    missing_columns: list[str] | None = None,
    duplicate_groups: int | None = None,
    duplicate_example: dict | None = None,
    null_pk_rows: int | None = None,
    pk_source: str | None = None,
    audit_path: str | None = None,
) -> str:
    """Build a detailed last_error_message for _meta_bronze_table_status."""
    lines = [
        f'Table: {schema_name}.{table_name} ({csv_stem})',
        f'Status: {status}',
    ]
    if audit_path:
        lines.append(f'Audit file: {audit_path}')

    if status == STATUS_PK_MISSING_IN_CSV:
        lines.append(
            f'Reason: Configured PK column(s) {missing_columns or []} '
            f'missing from CSV header.',
        )
        lines.append(
            f'Action: Update pk_config.json under "{schema_name}"."{table_name}" '
            f'with column names that exist in the CSV, then rerun sync.',
        )
    elif status == STATUS_PK_NOT_UNIQUE:
        lines.append(
            f'Reason: {duplicate_groups or "Multiple"} duplicate key group(s) '
            f'found using PK {pk_columns}.',
        )
        if duplicate_example:
            parts = [
                f'{k}={v!r}' for k, v in duplicate_example.items()
                if k != '_count'
            ]
            count = duplicate_example.get('_count', '?')
            lines.append(f'Example: {", ".join(parts)} appears {count} times.')
        if pk_source == 'auto_position_1':
            lines.append(
                f'Note: PK auto-detected as first column {pk_columns}. '
                f'Add explicit entry to pk_config.json if this is wrong.',
            )
        lines.append(
            f'Action: Update pk_config.json under "{schema_name}"."{table_name}" '
            f'with the correct composite primary key if merges look wrong.',
        )
    elif status == STATUS_PK_NULL_VALUES:
        lines.append(
            f'Reason: {null_pk_rows or "Some"} row(s) have NULL or empty PK '
            f'column(s) in {pk_columns}.',
        )
        lines.append(
            f'Action: Fix source data or adjust PK in pk_config.json for '
            f'"{schema_name}"."{table_name}", then rerun sync.',
        )
    else:
        if pk_source == 'auto_position_1':
            lines.append(
                f'PK auto-detected as first column {pk_columns} (source=auto_position_1).',
            )
        else:
            lines.append(f'PK {pk_columns} validation passed.')

    return '\n'.join(lines)


def classify_pk_header(
    header: list[str],
    pk_columns: list[str],
    pk_source: str,
) -> tuple[str | None, list[str]]:
    """Return (blocking_status, missing_columns). None status means header OK."""
    del pk_source  # auto-detect uses same header check as explicit config
    header_set = set(header or [])
    missing = [c for c in pk_columns if c not in header_set]
    if missing:
        return STATUS_PK_MISSING_IN_CSV, missing
    return None, []


def should_block_cdc_registration(status: str) -> bool:
    return status in PK_AUDIT_BLOCK_STATUSES


def resolve_audit_csv_path(
    csv_path: str,
    prefer_run_path: str | None = None,
) -> str:
    """Pick one CSV file for row-level PK checks (not a multi-run glob).

    CDC volumes store one folder per export run; globs like
    ``.../project_id/*/table.csv`` would falsely flag pk_not_unique when
    the same logical row appears in multiple runs.
    """
    path = (csv_path or '').replace('\\', '/')
    if '*' not in path and '?' not in path:
        return path

    csv_name = path.rstrip('/').split('/')[-1]
    if not csv_name.lower().endswith('.csv'):
        return path

    if prefer_run_path:
        run_dir = prefer_run_path.replace('\\', '/').rstrip('/')
        if run_dir.lower().endswith('.csv'):
            return run_dir
        single = f'{run_dir}/{csv_name}'
        if os.path.isfile(single):
            return single

    project_root = path.split('*')[0].rstrip('/')
    if not project_root:
        return path
    try:
        for run_name in sorted(os.listdir(project_root), reverse=True):
            candidate = f'{project_root}/{run_name}/{csv_name}'
            if os.path.isfile(candidate):
                return candidate
    except Exception:
        pass
    return path


def read_csv_header(spark, csv_path: str) -> list[str]:
    """Return CSV column names without loading full data."""
    df = (
        spark.read.format('csv')
        .option('header', True)
        .option('inferSchema', False)
        .load(csv_path)
        .limit(0)
    )
    cols = df.columns
    if not cols:
        raise ValueError(f'CSV header empty at {csv_path}')
    return cols


def audit_pk_csv(
    spark,
    csv_path: str,
    schema_name: str,
    table_name: str,
    csv_stem: str,
    pk_columns: list[str],
    pk_source: str,
    header: list[str] | None = None,
    prefer_run_path: str | None = None,
) -> tuple[str, str, list[str]]:
    """Audit one CSV for PK issues. Returns (status, error_message, header)."""
    from pyspark.sql import functions as F

    audit_path = resolve_audit_csv_path(csv_path, prefer_run_path)

    try:
        if header is None:
            header = read_csv_header(spark, audit_path)
    except FileNotFoundError:
        raise
    except Exception as exc:
        msg = str(exc)
        if 'does not exist' in msg.lower() or 'Path does not exist' in msg:
            raise FileNotFoundError(f'no files match {audit_path}') from exc
        return STATUS_CSV_UNREADABLE, msg, []

    header_status, missing = classify_pk_header(header, pk_columns, pk_source)
    if header_status == STATUS_PK_MISSING_IN_CSV:
        return (
            header_status,
            format_pk_audit_message(
                schema_name, table_name, csv_stem, header_status, pk_columns,
                missing_columns=missing, audit_path=audit_path,
            ),
            header,
        )

    try:
        df = (
            spark.read.format('csv')
            .option('header', True)
            .option('multiLine', True)
            .option('inferSchema', False)
            .option('mode', 'PERMISSIVE')
            .option('nullValue', '')
            .option('emptyValue', '')
            .load(audit_path)
            .select(*pk_columns)
        )
    except Exception as exc:
        return STATUS_CSV_UNREADABLE, str(exc), header

    null_cond = None
    for col_name in pk_columns:
        piece = F.col(col_name).isNull() | (F.trim(F.col(col_name)) == '')
        null_cond = piece if null_cond is None else (null_cond | piece)
    null_pk_rows = df.filter(null_cond).count() if null_cond is not None else 0
    if null_pk_rows > 0:
        return (
            STATUS_PK_NULL_VALUES,
            format_pk_audit_message(
                schema_name, table_name, csv_stem, STATUS_PK_NULL_VALUES,
                pk_columns, null_pk_rows=null_pk_rows, audit_path=audit_path,
            ),
            header,
        )

    grouped = df.groupBy(*pk_columns).count().filter(F.col('count') > 1)
    dup_sample = grouped.orderBy(F.desc('count')).limit(1).collect()
    if dup_sample:
        row = dup_sample[0].asDict()
        duplicate_groups = grouped.count()
        example = {k: row[k] for k in pk_columns}
        example['_count'] = row['count']
        return (
            STATUS_PK_NOT_UNIQUE,
            format_pk_audit_message(
                schema_name, table_name, csv_stem, STATUS_PK_NOT_UNIQUE,
                pk_columns,
                duplicate_groups=duplicate_groups,
                duplicate_example=example,
                pk_source=pk_source,
                audit_path=audit_path,
            ),
            header,
        )

    return (
        STATUS_OK,
        format_pk_audit_message(
            schema_name, table_name, csv_stem, STATUS_OK, pk_columns,
            pk_source=pk_source, audit_path=audit_path,
        ),
        header,
    )


def merge_table_status(
    spark,
    catalog: str,
    schema_bronze: str,
    schema_name: str,
    table_name: str,
    status: str,
    csv_header: list[str],
    error_msg: str | None = None,
) -> None:
    """MERGE one row into _meta_bronze_table_status."""
    from delta.tables import DeltaTable
    from pyspark.sql import functions as F
    from pyspark.sql.types import ArrayType, StringType, StructField, StructType

    column_diff_type = StructType([
        StructField('added', ArrayType(StringType())),
        StructField('absent_in_csv', ArrayType(StringType())),
        StructField('type_widened', ArrayType(StringType())),
        StructField('type_kept_old', ArrayType(StringType())),
    ])

    status_table = f'{catalog}.{schema_bronze}._meta_bronze_table_status'
    merge_keys = (
        'target.catalog = source.catalog AND '
        'target.`schema` = source.`schema` AND '
        'target.`table` = source.`table`'
    )
    source_df = (
        spark.createDataFrame(
            [(
                catalog,
                schema_name,
                table_name,
                status,
                error_msg,
                list(csv_header or []),
                ([], [], [], []),
            )],
            StructType([
                StructField('catalog', StringType()),
                StructField('schema', StringType()),
                StructField('table', StringType()),
                StructField('last_run_status', StringType()),
                StructField('last_error_message', StringType()),
                StructField('csv_header_at_last_run', ArrayType(StringType())),
                StructField('column_diff', column_diff_type),
            ]),
        )
        .withColumn('last_synced_at', F.current_timestamp())
    )
    (
        DeltaTable.forName(spark, status_table)
        .alias('target')
        .merge(source_df.alias('source'), merge_keys)
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
