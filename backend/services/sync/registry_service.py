
import logging
from backend.clients.databricks_client import DatabricksClient
logger = logging.getLogger(__name__)

###########################################

from .sync_config import (
    _META_BRONZE_SCHEMA,
    _META_REGISTRY_TABLE,
    _REGISTRY_SEED_BATCH,
)


from .schema_service import (
    _derive_position_1_pks,
    _sql_str,
)

##################################


def _seed_registry_from_schema(
    dbx: DatabricksClient,
    warehouse_id: str,
    catalog_name: str,
    schema_doc: dict,
) -> tuple[int, int]:
    """MERGE rows derived from schema.json into _meta_bronze_pk_registry.

    Existing rows are NEVER updated (PKs are immutable by design). New
    (schema, table) tuples are inserted with PK = column at
    ordinal_position 1 and source='auto_position_1'. Operator-curated PKs
    (source='manual_*') stay untouched on every subsequent sync.

    Returns (rows_in_schema, batches_executed).
    """
    rows = _derive_position_1_pks(schema_doc)
    if not rows:
        logger.warning('schema.json yielded zero (schema, table) rows — '
                       'registry update skipped')
        return 0, 0

    fqn_registry = (
        f'`{catalog_name}`.`{_META_BRONZE_SCHEMA}`.`{_META_REGISTRY_TABLE}`'
    )
    cat_sql = _sql_str(catalog_name)
    batches = 0
    for i in range(0, len(rows), _REGISTRY_SEED_BATCH):
        batch = rows[i:i + _REGISTRY_SEED_BATCH]
        values_clause = ', '.join(
            (
                f"('{cat_sql}', '{_sql_str(s)}', '{_sql_str(t)}', "
                f"array('{_sql_str(c)}'), current_timestamp(), 'auto_position_1')"
            )
            for s, t, c in batch
        )
        merge_sql = (
            f'MERGE INTO {fqn_registry} target '
            f'USING (SELECT * FROM (VALUES {values_clause}) AS '
            '       v(catalog, `schema`, `table`, pk_columns, locked_at, source)) source '
            'ON target.catalog = source.catalog '
            '   AND target.`schema` = source.`schema` '
            '   AND target.`table`  = source.`table` '
            'WHEN NOT MATCHED THEN INSERT *'
        )
        dbx.execute_sql(warehouse_id, merge_sql)
        batches += 1
        logger.info(
            'Registry seed: MERGEd batch %d-%d of %d',
            i + 1, i + len(batch), len(rows),
        )
    return len(rows), batches

