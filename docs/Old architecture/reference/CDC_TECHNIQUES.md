# Databricks CDC Techniques — ACC Connector Alignment

> Created: May 27, 2026  
> Purpose: Map [Lakeflow AUTO CDC](https://docs.databricks.com/aws/en/ldp/cdc) family APIs to ACC ingestion layers.  
> Related: [SCHEMA.md](../architecture/SCHEMA.md), [PLATFORM.md](../architecture/PLATFORM.md), [ACC_APIS.md](ACC_APIS.md)

---

## Technique cheat sheet

| Databricks API | Doc | Use when |
|----------------|-----|----------|
| **AUTO CDC FROM SNAPSHOT** | [AUTO CDC](https://docs.databricks.com/aws/en/ldp/cdc) | Source is **periodic full snapshots** only (DC Standard CSV drops) |
| **AUTO CDC** | [AUTO CDC](https://docs.databricks.com/aws/en/ldp/cdc) | Source is an explicit **change feed** (verb/operation + sequence column) |
| **AUTO CDC ONCE + continuous** | [RDBMS replication](https://docs.databricks.com/aws/en/ldp/database-replication) | Initial full snapshot, then ongoing CDC stream |
| **Append flow ONCE** | [Backfill](https://docs.databricks.com/aws/en/ldp/flows-backfill) | One-time historical load into existing streaming table |
| **REPLACE WHERE** (Beta) | [REPLACE WHERE](https://docs.databricks.com/aws/en/ldp/flows-replace-where) | Batch overwrite of a **predicate window** — not mixed with AUTO CDC on same table |
| **Advanced** | [Advanced AUTO CDC](https://docs.databricks.com/aws/en/ldp/cdc-advanced) | CDF from targets (DR 15.2+); `num_upserted_rows` metrics only for **AUTO CDC**, not FROM SNAPSHOT |

---

## What the connector implements today

| Layer | ACC source | Technique | Code |
|-------|------------|-----------|------|
| L1 / L4 bulk | DC Standard (`serviceGroups` all) | **AUTO CDC FROM SNAPSHOT** (SCD2) | [`acc-connector/notebooks/auto_cdc_pipeline.py`](acc-connector/notebooks/auto_cdc_pipeline.py) |
| Orchestration | Flask sync | Passes `acc.dc_snapshot_path` per run | [`acc-connector/backend/sync_service.py`](acc-connector/backend/sync_service.py) Phase 3 |
| Deletes | DC CSV | Hard delete via snapshot diff; soft via `deleted_at IS NULL` on source view | Same pipeline |
| PK gate | `autodesk_data_extract.zip` | Registry + header check | [`SCHEMA.md`](../architecture/SCHEMA.md) |

**Snapshot scoping (May 2026):** Each sync uploads to `…/data_connector/<project>/<timestamp>/`. The pipeline reads **only that folder** via `acc.dc_snapshot_path` so consecutive FROM SNAPSHOT runs compare the previous Bronze state to **one** new snapshot, not a union of all historical runs.

---

## Per-layer recommendation

| Connector layer | ACC source | Bronze technique | Notes |
|-----------------|------------|------------------|-------|
| L1 seed / L4 reconcile | DC Standard | **FROM SNAPSHOT** | Current production path |
| L3 activities | DC `activities` + date window | **AUTO CDC** | Verbs in `activity_verb`; see design below |
| L3 CDC beta | `cdcissues`, `cdccost`, … | **AUTO CDC** if delta CSV has operation + sequence; else **FROM SNAPSHOT** on delta files | Confirm columns in extract README |
| L2 REST (future) | Per-service REST / webhooks | **AUTO CDC** on change-shaped stream; optional Zerobus write | Do **not** apply FROM SNAPSHOT to raw list JSON |
| Historical backfill | Older DC folders in volume | **Append ONCE** or re-run FROM SNAPSHOT with explicit path | Use `acc.dc_snapshot_path` override |
| Selective recompute | Fix one window | **REPLACE WHERE** (Beta) on a **staging** table, or full snapshot re-run | Cannot share target with AUTO CDC flow |

---

## Design: DC activities → AUTO CDC (future)

DC **activities** rows are change events (`activity_verb`, `created_at`, `activity_id`), not full-table snapshots. Use **AUTO CDC**, not FROM SNAPSHOT.

Reference schema (offline): `acc-connector/schemas/activities.json` → `issues_activities` with PK `activity_id`, sequence `created_at`, verb column `activity_verb`.

Proposed flow (separate pipeline or notebook module — not implemented):

```python
# Pseudocode — validate verb → operation mapping against extract README first.

@dp.view(name='v_issues_activities')
def issues_activities_cdf():
    base = spark.conf.get('acc.dc_snapshot_path')  # or activities-specific path
    return spark.read.format('csv').option('header', True).load(f'{base}/issues_activities.csv')

dp.create_streaming_table('issues_activities_bronze')

dp.create_auto_cdc_flow(
    target='issues_activities_bronze',
    source='v_issues_activities',
    keys=['activity_id'],
    sequence_by=col('created_at'),
    apply_as_deletes=expr("activity_verb IN ('issue-deleted', 'issue.delete', ...)"),  # map from README
    stored_as_scd_type=2,
)
```

**Connector changes when implemented:**

1. Optional second DC request with `serviceGroups: ['activities']` and `startDate`/`endDate` (31-day max).
2. Land activities CSVs under a distinct volume prefix or same run folder with distinct names.
3. Register flows only for `*_activities` tables; keep standard tables on FROM SNAPSHOT.

---

## Design: REST re-introduction → change feed + AUTO CDC (future)

The removed realtime path must **not** write nested REST JSON directly into FROM SNAPSHOT tables (schema and grain differ from DC).

Recommended pattern:

1. **Fetch** — `GET /issues` (or webhook-triggered `GET /issues/{id}`) with watermark.
2. **Normalize** — Flattener emits rows matching **DC column names** for the target Bronze table (see `acc-connector/schemas/issues.json`).
3. **Land as changes** — Append to a staging volume path with synthetic columns:
   - `operation`: `INSERT` | `UPDATE` | `DELETE`
   - `sequence_num`: monotonic per sync (or use `updatedAt` epoch ms)
4. **Apply** — `create_auto_cdc_flow` into the same Bronze table as DC, **or** merge via Zerobus only if catalog is managed and rows are complete snapshots per key.

**Do not use** `create_auto_cdc_from_snapshot_flow` on per-poll JSON deltas.

**Zerobus** (future): no writer in the connector today. `ENABLE_ZEROBUS=true` only enables prerequisite diagnostics (`zerobus_status.py`); a gRPC writer would be new code + `databricks-sdk`.

See [REST_FUTURE.md](REST_FUTURE.md) and table 1.1 in [ACC_APIS.md](ACC_APIS.md).

---

## What we are not adopting (unless requirements change)

- **REPLACE WHERE** on tables already targeted by AUTO CDC FROM SNAPSHOT ([limitation](https://docs.databricks.com/aws/en/ldp/flows-replace-where)).
- **FROM SNAPSHOT** on REST poll results without materializing a full snapshot per entity type.
- Relying on pipeline metrics `num_upserted_rows` for snapshot flows — use `_meta_bronze_table_status` and `[SUMMARY]` instead ([advanced doc](https://docs.databricks.com/aws/en/ldp/cdc-advanced)).

---

## Implementation order

1. ~~Scope snapshot reads to one run folder (`acc.dc_snapshot_path`)~~ — done May 2026.
2. Keep bulk Standard extract on FROM SNAPSHOT.
3. Add activities / CDC-beta flows after validating column shapes in a real extract.
4. Re-introduce REST with change-feed landing + AUTO CDC (and optional Zerobus).
5. Evaluate CDF on Bronze targets (DR 15.2+) for Silver incremental consumption.
