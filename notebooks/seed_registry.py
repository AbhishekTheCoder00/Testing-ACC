# Databricks notebook source
# acc-connector — PK registry + schema.json seeder (Job task, NOT SDP).
#
# Runs after bulk_download / bulk_download_cdc and before snapshot_pipeline.
# SDP serverless pipelines cannot reliably persist Delta MERGE into
# _meta_bronze_pk_registry during planning; seeding here (plain Spark job)
# ensures gate_table_common() sees schema names and PK columns.
#
# Also runs CDC PK audit before Pipeline B — writes per-table status to
# _meta_bronze_table_status so bad PKs are caught before create_auto_cdc_flow.

# MAGIC %run ./shared/service_groups_config

# MAGIC %run ./shared/pk_audit_logic

# MAGIC %run ./shared/schema_api_loader

import hashlib
import io
import json
import os
import re
import zipfile

from delta.tables import DeltaTable
from pyspark.sql import functions as F

dbutils.widgets.text('acc_catalog', '')
dbutils.widgets.text('acc_dc_project_id', '')
dbutils.widgets.text('acc_dc_snapshot_path', '')
dbutils.widgets.text('acc_dc_cdc_path', '')

CATALOG = dbutils.widgets.get('acc_catalog').strip()
PROJECT_ID = dbutils.widgets.get('acc_dc_project_id').strip()
DC_SNAPSHOT_PATH = dbutils.widgets.get('acc_dc_snapshot_path').strip()
DC_CDC_PATH = dbutils.widgets.get('acc_dc_cdc_path').strip()

if not CATALOG:
    raise ValueError('acc_catalog widget is empty — pass via workflow notebook_params')

SCHEMA_BRONZE = 'bronze'
VOLUME_BASE = f'/Volumes/{CATALOG}/{SCHEMA_BRONZE}/acc_bronze_volume'
PK_CONFIG_PATH = f'{VOLUME_BASE}/pk_config.json'
SCHEMA_PATH = f'{VOLUME_BASE}/schema.json'
REGISTRY_TABLE = f'{CATALOG}.{SCHEMA_BRONZE}._meta_bronze_pk_registry'
SCHEMA_VERSIONS_TABLE = f'{CATALOG}.{SCHEMA_BRONZE}._meta_bronze_schema_versions'
STATUS_TABLE = f'{CATALOG}.{SCHEMA_BRONZE}._meta_bronze_table_status'
RAW_SNAPSHOTS_BASE = f'{VOLUME_BASE}/raw/snapshots'
DC_CDC_BASE = f'{VOLUME_BASE}/data_connector_cdc'
_MERGE_KEYS = (
    'target.catalog = source.catalog AND '
    'target.`schema` = source.`schema` AND '
    'target.`table` = source.`table`'
)
_PK_UPDATE = (
    "target.source IN ('auto_position_1', 'explicit_pk_config') AND "
    '(target.pk_columns != source.pk_columns OR target.source != source.source)'
)
_BATCH = 50
_DC_ZIP = 'autodesk_data_extract.zip'

DC_BASE = 'https://developer.api.autodesk.com/data-connector/v1'
DC_SCHEMA_ENDPOINT = f"{DC_BASE}/doc/schema"

DC_ALL_SERVICE_GROUPS = [
    'activities', 'admin', 'assets', 'cdcadmin', 'cdccost', 'cdciq',
    'cdcissues', 'cdclocations', 'cdcmarkups', 'cdcmeetingminutes',
    'cdcrelationships', 'cdcrfis', 'cdcschedule', 'cdcsheets',
    'cdcsubmittalsacc', 'cdctransmittals', 'checklists', 'clashes',
    'classifications', 'cost', 'dailylogs', 'estimates', 'forms', 'iq',
    'issues', 'issuesbim360', 'locations', 'markups', 'meetingminutes',
    'packages', 'photos', 'relationships', 'reviews', 'rfis', 'schedule',
    'sheets', 'submittals', 'submittalsacc', 'takeoff', 'transmittals',
]

print(
    f'[seed_registry] catalog={CATALOG} project_id={PROJECT_ID or "(unset)"} '
    f'snapshot_path={DC_SNAPSHOT_PATH or "(unset)"} cdc_path={DC_CDC_PATH or "(unset)"}'
)


def _fs_exists(path: str) -> bool:
    try:
        dbutils.fs.ls(path)
        return True
    except Exception:
        return False


def _read_bytes(path: str) -> bytes:
    row = spark.read.format('binaryFile').load(path).collect()[0]
    return bytes(row['content'])


def _is_three_level_schema_doc(doc: dict) -> bool:
    for tables in doc.values():
        if not isinstance(tables, dict):
            continue
        for cols in tables.values():
            if isinstance(cols, dict):
                for col_val in cols.values():
                    if isinstance(col_val, dict) and 'ordinal_position' in col_val:
                        return True
    return False


def _is_two_level_schema_doc(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    for tbl_val in doc.values():
        if not isinstance(tbl_val, dict):
            continue
        for col_val in tbl_val.values():
            return (
                isinstance(col_val, dict)
                and 'ordinal_position' in col_val
            )
    return False


def _schema_api_parallelism() -> int:
    try:
        return int(spark.conf.get('acc.schema_api_parallelism', '8'))
    except Exception:
        return DEFAULT_SCHEMA_API_PARALLELISM


def _load_schema_doc_from_api() -> dict:
    print(
        f'[seed_registry] Loading schema from APS Schema API '
        f'({len(DC_ALL_SERVICE_GROUPS)} groups, '
        f'parallelism={_schema_api_parallelism()})'
    )
    return load_schema_doc_from_api(
        DC_ALL_SERVICE_GROUPS,
        DC_SCHEMA_ENDPOINT,
        max_workers=_schema_api_parallelism(),
        timeout=DEFAULT_SCHEMA_API_TIMEOUT_SEC,
    )


def _seed_pk_registry() -> None:
    if not _fs_exists(PK_CONFIG_PATH):
        raise FileNotFoundError(
            f'pk_config.json missing at {PK_CONFIG_PATH} — re-run bootstrap'
        )

    pk_bytes = _read_bytes(PK_CONFIG_PATH)
    pk_config = json.loads(pk_bytes.decode('utf-8'))
    pk_hash = hashlib.sha256(pk_bytes).hexdigest()

    registry_count = int(
        spark.sql(f'SELECT COUNT(*) AS n FROM {REGISTRY_TABLE}').collect()[0]['n']
    )
    stored_hash = None
    try:
        prev = spark.sql(
            f"SELECT config_hash FROM {SCHEMA_VERSIONS_TABLE} "
            f"WHERE catalog = '{CATALOG}' AND config_type = 'pk_config'"
        ).collect()
        if prev:
            stored_hash = prev[0]['config_hash']
    except Exception:
        pass

    if stored_hash == pk_hash and registry_count > 0:
        print(f'[seed_registry] PK registry already seeded ({registry_count} rows). Skipping.')
        return

    rows: list[tuple[str, str, str, list, str]] = []
    for schema_name, tables in pk_config.items():
        if not isinstance(tables, dict):
            continue
        for table_name, pk_cols in tables.items():
            if isinstance(pk_cols, list) and pk_cols:
                rows.append((CATALOG, schema_name, table_name, list(pk_cols), 'explicit_pk_config'))

    if not rows:
        raise ValueError(f'pk_config.json at {PK_CONFIG_PATH} yielded zero tables')

    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        source_df = (
            spark.createDataFrame(batch, ['catalog', 'schema', 'table', 'pk_columns', 'source'])
            .withColumn('locked_at', F.current_timestamp())
        )
        (
            DeltaTable.forName(spark, REGISTRY_TABLE)
            .alias('target')
            .merge(source_df.alias('source'), _MERGE_KEYS)
            .whenMatchedUpdate(
                condition=_PK_UPDATE,
                set={
                    'pk_columns': 'source.pk_columns',
                    'source': 'source.source',
                    'locked_at': 'source.locked_at',
                },
            )
            .whenNotMatchedInsertAll()
            .execute()
        )

    proj = PROJECT_ID or 'unknown'
    hash_df = (
        spark.createDataFrame(
            [(CATALOG, proj, 'pk_config', pk_hash, PK_CONFIG_PATH)],
            ['catalog', 'project_id', 'config_type', 'config_hash', 'config_path'],
        )
        .withColumn('last_applied_at', F.current_timestamp())
    )
    (
        DeltaTable.forName(spark, SCHEMA_VERSIONS_TABLE)
        .alias('target')
        .merge(
            hash_df.alias('source'),
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

    final = int(spark.sql(f'SELECT COUNT(*) AS n FROM {REGISTRY_TABLE}').collect()[0]['n'])
    print(f'[seed_registry] OK PK registry seeded: {final} rows from pk_config.json')


def _load_schema_from_zip(zip_path: str) -> dict | None:
    """Open the Autodesk extract ZIP and return one normalized
    ``{schema: {table: {column: {...}}}}`` dict.

    Two layouts are supported, in order of preference:

    1. ``schemas/schema.json`` — single consolidated file. Returned
       directly if its shape matches the three-level model.
    2. ``schemas/<domain>.json`` (one file per ACC domain). Each file is
       a two-level ``{table: {column: {...}}}`` doc; the filename stem
       becomes the schema name in the merged result.

    Returns None when the ZIP is missing, unreadable, or contains no
    recognizable schema files.
    """
    if not _fs_exists(zip_path):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(_read_bytes(zip_path))) as zf:
            names = zf.namelist()

            # Layout 1 — consolidated schemas/schema.json.
            consolidated = [
                n for n in names
                if n.lower().endswith('/schema.json') or n.lower() == 'schema.json'
            ]
            for member in consolidated:
                try:
                    doc = json.loads(zf.read(member).decode('utf-8'))
                except json.JSONDecodeError as exc:
                    print(f'[seed_registry] WARN {member} in {zip_path} is not valid JSON: {exc}')
                    continue
                if _is_three_level_schema_doc(doc):
                    print(f'[seed_registry] Loaded consolidated schema from {member} in {zip_path}')
                    return doc

            # Layout 2 — per-domain schemas/<domain>.json.
            per_domain = [
                n for n in names
                if n.lower().startswith('schemas/')
                and n.lower().endswith('.json')
                and not n.lower().endswith('/schema.json')
                and not n.endswith('/')
            ]
            merged: dict = {}
            for member in per_domain:
                domain = member.rsplit('/', 1)[-1]
                domain = domain[:-len('.json')] if domain.lower().endswith('.json') else domain
                try:
                    doc = json.loads(zf.read(member).decode('utf-8'))
                except json.JSONDecodeError as exc:
                    print(f'[seed_registry] WARN {member} in {zip_path} is not valid JSON: {exc}')
                    continue
                if _is_two_level_schema_doc(doc):
                    merged[domain] = doc
                elif _is_three_level_schema_doc(doc):
                    for sch, tbls in doc.items():
                        merged.setdefault(sch, tbls)

            if merged:
                print(f'[seed_registry] Loaded per-domain schema from {len(per_domain)} file(s) in {zip_path}')
                return merged
    except Exception as exc:
        print(f'[seed_registry] WARN zip read failed {zip_path}: {exc}')
    return None


def _read_schema_from_volume() -> dict | None:
    if not _fs_exists(SCHEMA_PATH):
        return None
    try:
        return json.loads(_read_bytes(SCHEMA_PATH).decode('utf-8'))
    except Exception as exc:
        print(f'[seed_registry] WARN could not read existing {SCHEMA_PATH}: {exc}')
        return None


def _write_schema_to_volume(schema_doc: dict, source: str) -> None:
    dbutils.fs.put(SCHEMA_PATH, json.dumps(schema_doc, indent=2), overwrite=True)
    schema_bytes = json.dumps(schema_doc, sort_keys=True).encode('utf-8')
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()
    proj = PROJECT_ID or 'unknown'
    hash_df = (
        spark.createDataFrame(
            [(CATALOG, proj, 'schema_json', schema_hash, SCHEMA_PATH)],
            ['catalog', 'project_id', 'config_type', 'config_hash', 'config_path'],
        )
        .withColumn('last_applied_at', F.current_timestamp())
    )
    (
        DeltaTable.forName(spark, SCHEMA_VERSIONS_TABLE)
        .alias('target')
        .merge(
            hash_df.alias('source'),
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
    n_tables = sum(len(v) for v in schema_doc.values() if isinstance(v, dict))
    print(f'[seed_registry] OK schema.json written ({n_tables} tables) from {source}')


def _ensure_schema_json() -> None:
    # --- Step 1: Try APS Schema API first ---
    api_doc = None
    try:
        api_doc = _load_schema_doc_from_api()
        print('[seed_registry] Schema API succeeded.')
    except Exception as exc:
        print(f'[seed_registry] Schema API unavailable: {exc}')

    if api_doc is not None:
        existing = _read_schema_from_volume()
        if existing is not None:
            print('[seed_registry] Existing schema found in Volume. Comparing API schema with Volume schema...')
            if json.dumps(api_doc, sort_keys=True) == json.dumps(existing, sort_keys=True):
                print('[seed_registry] Schemas are identical — refreshing schema.json in Volume with latest API schema.')
            else:
                print('[seed_registry] Volume schema differs. Updating schema.json in Volume.')
        else:
            print('[seed_registry] Volume schema.json not found. Uploading new schema.')
        _write_schema_to_volume(api_doc, 'APS Schema API')
        return

    # --- Step 2: API failed — fall back to ZIP ---
    print('[seed_registry] Loading schema from ZIP...')
    zip_doc = None
    used = None
    zip_paths = []
    if DC_SNAPSHOT_PATH:
        zip_paths.append(f'{DC_SNAPSHOT_PATH.rstrip("/")}/{_DC_ZIP}')
    if DC_CDC_PATH:
        zip_paths.append(f'{DC_CDC_PATH.rstrip("/")}/{_DC_ZIP}')
    for zp in zip_paths:
        doc = _load_schema_from_zip(zp)
        if doc:
            zip_doc = doc
            used = zp
            break

    if zip_doc is not None:
        existing = _read_schema_from_volume()
        if existing is not None:
            print('[seed_registry] Existing schema found in Volume. Comparing ZIP schema with Volume schema...')
            if json.dumps(zip_doc, sort_keys=True) == json.dumps(existing, sort_keys=True):
                print('[seed_registry] Schemas are identical — refreshing schema.json in Volume with latest ZIP schema.')
            else:
                print('[seed_registry] Volume schema differs. Updating schema.json in Volume.')
        else:
            print('[seed_registry] Volume schema.json not found. Uploading new schema.')
        _write_schema_to_volume(zip_doc, f'ZIP ({used})')
        return

    # --- Step 3: Nothing available ---
    if _fs_exists(SCHEMA_PATH):
        print(f'[seed_registry] No API or ZIP — using existing {SCHEMA_PATH}')
        return

    print(
        '[seed_registry] WARN no schema.json from API or ZIP — pipeline may skip tables '
        'without schema evolution metadata'
    )


def _table_name_from_csv(filename: str) -> str:
    stem = filename[:-4] if filename.lower().endswith('.csv') else filename
    return re.sub(r'[^a-z0-9_]', '_', stem.lower())


def _load_registry_lookup() -> dict:
    lookup: dict[str, dict] = {}
    try:
        rows = spark.sql(
            f'SELECT `schema`, `table`, pk_columns, source FROM {REGISTRY_TABLE}',
        ).collect()
    except Exception as exc:
        print(f'[seed_registry] WARN registry read failed: {exc}')
        return lookup
    for row in rows:
        sch = row['schema']
        tbl = row['table']
        lookup[f'{sch}_{tbl}'.lower()] = {
            'schema': sch,
            'table': tbl,
            'pk_columns': list(row['pk_columns'] or []),
            'source': row['source'] or 'unknown',
        }
    return lookup


def _resolve_project_cdc_root(cdc_run_path: str) -> str:
    run_path = cdc_run_path.rstrip('/')
    if not run_path:
        return ''
    return os.path.dirname(run_path)


def _discover_cdc_csv_paths() -> list[tuple[str, str, str]]:
    """Return (csv_name, csv_path, service_group) for CDC audit."""
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    project_root = _resolve_project_cdc_root(DC_CDC_PATH)
    if project_root and os.path.isdir(project_root):
        try:
            for run_name in os.listdir(project_root):
                sub = os.path.join(project_root, run_name)
                if not os.path.isdir(sub):
                    continue
                for fn in os.listdir(sub):
                    if not fn.lower().endswith('.csv'):
                        continue
                    grp = cdc_group_for_csv_stem(_table_name_from_csv(fn))
                    if not grp:
                        continue
                    key = (grp, fn)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append((fn, os.path.join(sub, fn), grp))
        except Exception as exc:
            print(f'[seed_registry] WARN CDC CSV discovery failed: {exc}')

    if os.path.isdir(RAW_SNAPSHOTS_BASE):
        for grp in sorted(DELTA_CDC_GROUPS):
            grp_dir = os.path.join(RAW_SNAPSHOTS_BASE, grp)
            if not os.path.isdir(grp_dir):
                continue
            try:
                for fn in os.listdir(grp_dir):
                    if not fn.lower().endswith('.csv'):
                        continue
                    key = (grp, fn)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append((fn, os.path.join(grp_dir, fn), grp))
            except Exception as exc:
                print(f'[seed_registry] WARN list {grp_dir}: {exc}')

    if DC_SNAPSHOT_PATH and os.path.isdir(DC_SNAPSHOT_PATH):
        try:
            for fn in os.listdir(DC_SNAPSHOT_PATH):
                if not fn.lower().endswith('.csv'):
                    continue
                grp = cdc_group_for_csv_stem(_table_name_from_csv(fn))
                if not grp:
                    continue
                key = (grp, fn)
                if key in seen:
                    continue
                seen.add(key)
                found.append((fn, os.path.join(DC_SNAPSHOT_PATH, fn), grp))
        except Exception as exc:
            print(f'[seed_registry] WARN snapshot path list failed: {exc}')

    return sorted(found, key=lambda x: (x[2], x[0]))


def _run_cdc_pk_audit() -> None:
    registry = _load_registry_lookup()
    csv_entries = _discover_cdc_csv_paths()
    if not csv_entries:
        print('[seed_registry] PK audit — no CDC CSVs found on volume this run')
        return

    counts: dict[str, int] = {}
    for csv_name, csv_path, grp in csv_entries:
        table_name = _table_name_from_csv(csv_name)
        flat_key = table_name.lower()
        if flat_key in registry:
            sch = registry[flat_key]['schema']
            tbl = registry[flat_key]['table']
            pk_columns = registry[flat_key]['pk_columns']
            pk_source = registry[flat_key]['source']
        else:
            sch, tbl = schema_from_table_name(table_name, set(DELTA_CDC_GROUPS))
            if sch == 'unknown':
                print(f'[seed_registry] PK audit skip {csv_name} — not in registry')
                continue
            pk_source = 'auto_position_1'
            try:
                header = read_csv_header(spark, csv_path)
            except Exception:
                continue
            pk_columns = [header[0]] if header else []

        if not pk_columns:
            continue

        try:
            status, err_msg, header = audit_pk_csv(
                spark, csv_path, sch, tbl, table_name,
                pk_columns, pk_source,
                prefer_run_path=DC_CDC_PATH,
            )
        except FileNotFoundError:
            print(f'[seed_registry] PK audit skip {sch}.{tbl} — file missing')
            continue
        except Exception as exc:
            status = STATUS_CSV_UNREADABLE
            err_msg = str(exc)
            header = []

        merge_table_status(
            spark, CATALOG, SCHEMA_BRONZE, sch, tbl,
            status, header, error_msg=err_msg if status != STATUS_OK else None,
        )
        counts[status] = counts.get(status, 0) + 1
        level = 'ERROR' if status in PK_AUDIT_BLOCK_STATUSES else 'INFO'
        print(f'[seed_registry] PK audit [{level}] {sch}.{tbl}: {status}')

    summary = ', '.join(f'{k}={v}' for k, v in sorted(counts.items()))
    print(f'[seed_registry] PK audit summary: {summary or "no tables audited"}')


_seed_pk_registry()
_ensure_schema_json()
_run_cdc_pk_audit()
print('[seed_registry] DONE — snapshot_pipeline may start')
