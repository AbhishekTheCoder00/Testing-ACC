# Testing the ACC Connector (two-pipeline ingestion)

> May 2026 — snapshot (`FROM SNAPSHOT`) + daily CDC (`AUTO CDC`).  
> Design lock: [PLATFORM.md](../architecture/PLATFORM.md)

---

## 0. Offline smoke (no ACC/Databricks)

From `acc-connector/`:

```powershell
python -m py_compile backend/sync_service.py backend/acc_client.py app.py notebooks/auto_cdc_pipeline.py notebooks/auto_cdc_cdc_pipeline.py
python scripts/smoke_check.py
```

---

## 1. Prerequisites

| Item | Check |
|------|--------|
| `.env` | Copy from `.env.example` — APS + Databricks OAuth vars |
| Python 3.11+ | `pip install -r requirements.txt` |
| ACC test user | Hub/project with Data Connector access |
| Databricks | Pro/Advanced or Serverless (pipelines), SQL warehouse |
| **Re-bootstrap** | Required after CDC pipeline was added — creates `cdc_pipeline_id` |

---

## 2. Start the app

```powershell
cd acc-connector
.\start.bat
```

Open http://localhost:8000

**Wizard order:** Sign in Autodesk → pick hub/project → Sign in Databricks → bootstrap catalog → Sync.

---

## 3. Test A — Bootstrap (both pipelines)

1. Step 2: connect Databricks, pick a **managed catalog with explicit storage** (not Default Storage only).
2. Run bootstrap; wait for **complete**.
3. In Databricks UI → **Workflows → Lakeflow Pipelines**, confirm:
   - `CCTech_ACCConnector_Bronze` (snapshot)
   - `CCTech_ACCConnector_Bronze_CDC` (CDC)
4. Optional SQL:

```sql
SELECT * FROM <catalog>.bronze._meta_bronze_pk_registry LIMIT 5;
```

Dashboard → Databricks context should show `bronze_pipeline_id` and `cdc_pipeline_id` links.

---

## 3.5 Test typed schema ingestion

After bootstrap and first sync, verify typed Bronze ingestion:

1. **Inspect pk_config.json in Volume:**

```python
# In Databricks notebook
dbutils.fs.head('/Volumes/<catalog>/bronze/acc_bronze_volume/pk_config.json')
```

Expected: JSON with PK definitions per schema/table.

2. **Run first sync → check Bronze table schema:**

```sql
DESCRIBE TABLE EXTENDED <catalog>.bronze.<table_name>;
```

Verify columns have correct types (INT, DOUBLE, TIMESTAMP, not all STRING).

3. **Check for schema evolution details:**

```sql
SELECT `schema`, `table`,
       column_diff.type_widened,
       column_diff.type_kept_old,
       column_diff.added,
       column_diff.absent_in_csv
FROM <catalog>.bronze._meta_bronze_table_status
WHERE SIZE(column_diff.type_widened) > 0
   OR SIZE(column_diff.type_kept_old) > 0
   OR SIZE(column_diff.added) > 0;
```

Expected: Empty on first run. On subsequent runs after `schema.json` changes,
shows which tables had columns added, types widened, or unsafe types kept.

4. **Test empty value handling:**

- Verify CSVs with empty numeric columns ingest as NULL (not parse errors).
- Check ingestion didn't skip tables due to empty values:

```sql
SELECT `schema`, `table`, last_run_status, last_error_message
FROM <catalog>.bronze._meta_bronze_table_status
WHERE last_run_status != 'ok';
```

Expected: No `csv_unreadable` errors from empty values.

5. **Verify config hash tracking:**

```sql
SELECT config_type, config_hash, last_applied_at
FROM <catalog>.bronze._meta_bronze_schema_versions
ORDER BY last_applied_at DESC;
```

Expected: Rows for both `pk_config` and `schema_json`.

6. **Test pk_config registry upsert** — edit one PK in
   `config/pk_config_template.json`, upload to Volume as `pk_config.json`,
   run pipeline → driver log should show `N inserted, M updated` and
   WARN if PK columns changed. Verify:

```sql
SELECT schema, table, pk_columns, source
FROM <catalog>.bronze._meta_bronze_pk_registry
WHERE schema = 'issues' AND table = 'issues';
```

Second pipeline run without changing the file → log
`pk_config.json unchanged - PK registry sync skipped`.

7. **Q2-C Controlled column-add test (run once to verify DLT behavior):**

   a. Pick a small Bronze table (e.g., `admin_users`).  
   b. In `schema.json` on the Volume, add a fake column to that table's definition:
      ```json
      "test_new_col": {"data_type": "string", "ordinal_position": 999}
      ```
   c. Update the SHA-256 hash so the pipeline sees a change (or just modify the file).  
   d. Run the pipeline.  
   e. Check whether the new column appears in the Bronze Delta table:
      ```sql
      DESCRIBE TABLE EXTENDED <catalog>.bronze.admin_users;
      ```
   f. If the column appears: DLT handles `schema=` additions on existing SCD2
      targets without full refresh. Document result.  
   g. If the column does NOT appear: a full refresh of that pipeline table
      is needed after schema additions. Document and plan accordingly.  
   h. Clean up: remove the fake column from `schema.json`.

---

## 4. Test B — Manual full sync (snapshot path)

1. Step 3 → **Sync Now**.
2. Watch status until `complete` (can take 30+ min for large projects).
3. Verify in Databricks **Catalog** → volume:

   `…/acc_bronze_volume/data_connector/<project_id>/<timestamp>/`

   - CSV files + `autodesk_data_extract.zip`
4. Open latest **snapshot pipeline** run → `[SUMMARY]` in driver logs: `ingested_clean` > 0 for tables with CSVs.
5. Query one Bronze table, e.g.:

```sql
SELECT COUNT(*) FROM <catalog>.bronze.<table_from_csv_stem>;
```

6. **24h gate:** click **Sync Now** again within 24h → run should **fail** with manual full rate-limit message (`GET /sync/status` → `manual_full_next_at`).
7. **Pipeline guard:** start a pipeline update in Databricks UI, then **Sync Now** → should fail with “pipeline is running”.

---

## 5. Test C — Daily CDC sync

1. Dashboard → **Enable Daily CDC Sync** (requires `cdc_pipeline_id` from bootstrap).
2. Wait for scheduled run (24h) **or** trigger CDC manually from a Python shell (below).
3. Verify volume path:

   `…/data_connector_cdc/<project_id>/<timestamp>/`

4. Open **CDC pipeline** run → per-table `OK` lines with `seq=adsk_updated_at` (or `updated_at`).
5. Confirm **same** Bronze table name as snapshot path (not a `*_cdc` sibling table).

**Manual CDC trigger (dev):**

```powershell
cd acc-connector
python -c "from backend import sync_service; sync_service.run_cdc_sync('YOUR_USER_ID', trigger_type='auto')"
```

Replace `YOUR_USER_ID` with the session user id from `connector.db` (`SELECT user_id FROM acc_config`).

---

## 6. Test D — POC overlap table (recommended)

Pick one CDC-beta table (e.g. issues domain) after **Test B** then **Test C**:

| Check | Pass criteria |
|-------|----------------|
| Row count | CDC adds/changes rows; SCD2 has `__START_AT` / `__END_AT` if enabled |
| Deletes | Rows with `deleted_at` in CDC CSV → tombstoned / ended in SCD2 |
| Reconcile | Optional second manual full (after 24h or reset watermark) — table still one logical entity |

---

## 7. Observability

| Surface | What to look at |
|---------|----------------|
| Flask logs | `Sync run N started (mode=snapshot\|cdc)` |
| `GET /sync/status` | `daily_cdc_enabled`, `manual_full_next_at`, latest `run` |
| `GET /dashboard/data` | `cdc_pipeline_id`, sync history trigger `Daily CDC` / `Manual full` |
| `<catalog>.bronze._meta_bronze_table_status` | Per-table `last_run_status` |

---

## 8. Reset watermarks (dev only)

To re-test manual full within 24h, delete watermark rows in `connector.db`:

```sql
-- sqlite3 connector.db
DELETE FROM watermarks WHERE data_type = 'data_connector_manual_full';
```

Or delete `connector.db` and re-authenticate (loses tokens).

---

## 9. Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| CDC toggle fails | Re-bootstrap — missing `cdc_pipeline_id` |
| All CDC tables skipped | No `adsk_updated_at` / `updated_at` in CSV — check real extract README |
| Pipeline 400 on create | Workspace edition — need Advanced + serverless for pipelines |
| Empty Bronze | PK missing in CSV header vs registry — check `_meta_bronze_table_status` |
| Sync stuck on pipeline | Databricks pipeline failed — open run for error |

---

## 10. Not covered by automated tests

There is no pytest suite yet. Validation is **manual E2E** against live ACC + Databricks per this guide.
