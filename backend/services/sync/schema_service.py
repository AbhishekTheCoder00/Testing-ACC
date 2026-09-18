
import io
import json
import logging
import zipfile
import json



logger = logging.getLogger(__name__)

############################

from .sync_config import (
    _DC_CONSOLIDATED_NAME,
    _DC_SCHEMAS_DIR,
    _DC_EXTRACT_ZIP_NAME,
)

#########################

# ---------------------------------------------------------------------------
# PK registry seeding from autodesk_data_extract.zip
# ---------------------------------------------------------------------------


def _sql_str(s: str) -> str:
    """Escape a STRING literal for inline SQL. ACC schema/table/column
    names are alphanumeric + underscore (validated by Autodesk), so this
    is defense-in-depth, not a security boundary."""
    return s.replace("'", "''")


def _derive_position_1_pks(schema_doc: dict) -> list[tuple[str, str, str]]:
    """For every (schema, table) declared in schema.json, return
    (schema, table, pk_col) where pk_col is the column with the lowest
    ordinal_position. No blocklists — whatever the tenant's extract
    declares is authoritative.
    """
    rows: list[tuple[str, str, str]] = []
    for sch_name, tables in schema_doc.items():
        if not isinstance(tables, dict):
            continue
        for tbl_name, columns in tables.items():
            if not isinstance(columns, dict) or not columns:
                continue
            try:
                first_col = min(
                    columns.items(),
                    key=lambda kv: (
                        kv[1].get('ordinal_position', 9999)
                        if isinstance(kv[1], dict) else 9999
                    ),
                )[0]
            except ValueError:
                continue
            rows.append((sch_name, tbl_name, first_col))
    return rows


def _is_three_level_schema_doc(doc: dict) -> bool:
    """Return True when ``doc`` looks like a consolidated schema.json:
    three-level nesting where leaves carry an ``ordinal_position``.

    Sampling-based: we only inspect the first table's first column to
    avoid walking the whole document on every detection call.
    """
    if not isinstance(doc, dict):
        return False
    for sch_val in doc.values():
        if not isinstance(sch_val, dict):
            continue
        for tbl_val in sch_val.values():
            if not isinstance(tbl_val, dict):
                continue
            for col_val in tbl_val.values():
                return (
                    isinstance(col_val, dict)
                    and 'ordinal_position' in col_val
                )
    return False


def _is_two_level_schema_doc(doc: dict) -> bool:
    """Return True when ``doc`` looks like a per-domain JSON
    (``admin.json``, ``issues.json``, …): two-level nesting where leaves
    carry ``ordinal_position`` directly under each column.
    """
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

def _load_schema_doc_from_zip(zip_bytes: bytes | None) -> dict | None:
    """Open autodesk_data_extract.zip in memory and return one
    normalized ``{schema: {table: {column: {...}}}}`` dict.

    Two layouts are supported, in order of preference:

    1. ``schemas/schema.json`` — single consolidated file. Returned
       directly if its shape matches Section 1's three-level model.
    2. ``schemas/<domain>.json`` (one file per ACC domain). Each file is
       a two-level ``{table: {column: {...}}}`` doc; the filename stem
       becomes the schema name in the merged result.

    Returns None when the zip is unreadable, contains no recognizable
    schema files, or every candidate fails to parse. Callers warn and
    fall back to the pipeline's per-CSV defensive path.
    """
    if not zip_bytes:
        logger.warning(
            'No %s bytes supplied — cannot load schema from ZIP',
            _DC_EXTRACT_ZIP_NAME,
        )
        return None

    logger.info("Loading schema from Autodesk extract ZIP.")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:             #zipfile.ZipFile(io.BytesIO(zip_bytes)) opens the zip file in memory. io.BytesIO(zip_bytes) creates a file-like object from the bytes of the zip file. zf is the zip file object that allows us to read the contents of the zip file without extracting it to disk.
            names = zf.namelist()                                      # list of all the file names in the zip file. For example, if the zip file contains "schemas/schema.json" and "schemas/admin.json", then names = ["schemas/schema.json", "schemas/admin.json"]

            # Layout 1 — consolidated schemas/schema.json (or any path
            # ending in /schema.json or just schema.json at the root).
            consolidated = [                                            # Suppose schemas/schema.json exists Then consolidated   becomes  ["schemas/schema.json"]  If it doesn't exist []
                n for n in names
                if n.lower().endswith('/' + _DC_CONSOLIDATED_NAME)      # _DC_CONSOLIDATED_NAME = 'schema.json'
                or n.lower() == _DC_CONSOLIDATED_NAME
            ]
            for member in consolidated:                                 # if schemas/schema.json exists Then member = "schemas/schema.json"  If it doesn't exist []  Then consolidated = []  and this loop is skipped
                try:
                    with zf.open(member) as f:                          # Open the file only schema.json inside the zip. It does not extract it to disk. Everything stays in memory.
                        doc = json.loads(f.read().decode('utf-8'))      # Let's split this  First f.read() read {.....} as bytes b'{...} ' then decode('utf-8') converts it to a string '{...}' then json.loads() converts it to a dict {....}
                except json.JSONDecodeError as exc:
                    logger.warning('%s in %s is not valid JSON: %s',
                                   member, _DC_EXTRACT_ZIP_NAME, exc)   #_DC_EXTRACT_ZIP_NAME = 'autodesk_data_extract.zip'
                    continue
                if _is_three_level_schema_doc(doc):                     # its checks if the doc is a three-level schema doc. If it is, then it returns the doc. If not, it continues to the next member in consolidated    schema -> table -> colum 
                    logger.info(
                        'Loaded consolidated schema from %s in %s',
                        member, _DC_EXTRACT_ZIP_NAME,
                    )
                    return doc

            # Layout 2 — per-domain schemas/<domain>.json.
            per_domain = [
                n for n in names
                if n.lower().startswith(_DC_SCHEMAS_DIR)                 #_DC_SCHEMAS_DIR = 'schemas/'
                and n.lower().endswith('.json')
                and not n.lower().endswith('/' + _DC_CONSOLIDATED_NAME)  #_DC_CONSOLIDATED_NAME = 'schema.json'
                and not n.endswith('/')                                  # skip directory entries
            ]



            
            merged: dict = {}                                            #creates an empty dictionary.
            for member in per_domain:                                    # Loop starts First schemas/admin.json  Domain becomes admin
                domain = member.rsplit('/', 1)[-1]                       # gets the last part of the path after the last '/' which is the filename. For example, if member = "schemas/admin.json" then domain = "admin.json"
                domain = domain[:-len('.json')] if domain.lower().endswith('.json') else domain   #removes the '.json' extension from the domain name. For example, if domain = "admin.json" then domain becomes "admin" . read admin.json  convert   ---->  dictionary  {table: {column: {...}}}  then merged[domain] = doc  becomes merged['admin'] = {table: {column: {...}}}
                try:
                    with zf.open(member) as f:                             # Open the file only admin.json inside the zip. It does not extract it to disk. Everything stays in memory.
                        doc = json.loads(f.read().decode('utf-8'))         # let's split this  First f.read() read {.....} as bytes b'{...} ' then decode('utf-8') converts it to a string '{...}' then json.loads() converts it to a dict {....}
                except json.JSONDecodeError as exc:                        # except block catches the JSONDecodeError exception that may occur if the file is not valid JSON. If this happens, it logs a warning message and continues to the next member in per_domain.
                    logger.warning('%s in %s is not valid JSON: %s',
                                   member, _DC_EXTRACT_ZIP_NAME, exc) 
                    continue
                if _is_two_level_schema_doc(doc):                        # checks if the doc is a two-level schema doc. If it is, then it adds the doc to the merged dictionary with the domain name as the key. If not, it checks if it's a three-level schema doc and merges it accordingly.  table -> column -> {ordinal_position: ...}
                    merged[domain] = doc                                 #if yes  insert  merged["admin"]=doc  now  merged becomes {"admin": {table: {column: {...}}}}  next read issues.json insert merged["issues"]=doc  now merged becomes {"admin": {table: {column: {...}}}, "issues": {table: {column: {...}}}}
                elif _is_three_level_schema_doc(doc):
                    # Some zips ship a sub-domain file that's already
                    # nested ({schema: {table: ...}}). Merge by union;
                    # later files don't overwrite earlier ones.
                    for sch, tbls in doc.items():
                        merged.setdefault(sch, tbls)

            if merged:
                logger.info(
                    'Loaded per-domain schema from %d file(s) in %s',
                    len(per_domain), _DC_EXTRACT_ZIP_NAME,              #_DC_EXTRACT_ZIP_NAME = 'autodesk_data_extract.zip'
                )
                return merged

            logger.warning(
                '%s contains no recognizable schema files (looked for '
                '%s%s and %s<domain>.json) — PK registry will not be '
                'updated this run',
                _DC_EXTRACT_ZIP_NAME, _DC_SCHEMAS_DIR,
                _DC_CONSOLIDATED_NAME, _DC_SCHEMAS_DIR,
            )
            return None
    except zipfile.BadZipFile as exc:
        logger.warning(
            'Cannot read %s as a zip archive: %s — PK registry will '
            'not be updated this run', _DC_EXTRACT_ZIP_NAME, exc,
        )
        return None
