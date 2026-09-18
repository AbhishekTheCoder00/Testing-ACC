# ACC Service Groups — CDC Pipeline Architecture

**Document Version:** 1.0  
**Date:** July 2026  
**Author:** Data Engineering Team  

---

## 1. Objective

- Create Delta tables for **25 service groups** of ACC (~262 tables)
- Daily update of Delta tables for **12 service groups** which provide delta CSVs
- Initial full snapshot (historical extraction) for all 25 groups

---

## 2. Source Details

| Source | Description |
| --- | --- |
| 25 service groups | Bulk export CSVs (~262 CSVs total) |
| 12 of 25 groups | Also provide daily delta CSVs (change records) |
| 13 of 25 groups | Snapshot-only (no delta/CDC capability) |

### Source Layout (Volumes)

```
/Volumes/acc/raw/
├── snapshots/              ← Full exports (all 25 groups)
│   ├── service_group_01/
│   │   ├── table_a.csv
│   │   ├── table_b.csv
│   ├── service_group_02/
│   ...
├── deltas/                 ← Daily delta CSVs (12 groups only)
│   ├── service_group_14/
│   │   ├── 2024-01-15/table_a_delta.csv
│   │   ├── 2024-01-15/table_b_delta.csv
│   ├── service_group_15/
│   ...
```

---

## 3. Final Architecture Decision

**Option B: 2 Pipelines + 1 Catalog + Serverless (90% correct for production)**

| Decision | Answer | Why |
| --- | --- | --- |
| Pipelines | 2 (split by method) | Different schedules (weekly vs daily), failure isolation, faster initialization |
| Catalog | 1 shared (`acc`) | Same domain, consumers query together, simple permissions |
| Schema | 1 shared (`bronze`) | All 262 tables accessible as `acc.bronze.table_name` |
| Cluster | Serverless | No sizing guesswork for 262 tables, auto-scales, no idle cost |

---

## 4. Pipeline Design

### Pipeline A: ACC Snapshot Pipeline

| Property | Value |
| --- | --- |
| Method | `create_auto_cdc_from_snapshot_flow()` |
| Service Groups | 13 (snapshot-only) |
| Tables | ~130 |
| Schedule | Weekly / On-demand |
| Mode | Triggered |
| Target | `acc.bronze` |

**How it works:**
- Reads full CSV snapshot from volume path
- Compares current snapshot vs previous snapshot automatically
- Detects INSERTs, UPDATEs, DELETEs by diffing
- Applies SCD Type 1 (overwrites with latest)

### Pipeline B: ACC Delta CDC Pipeline

| Property | Value |
| --- | --- |
| Method | `create_auto_cdc_flow()` × 2 flows per table |
| Service Groups | 12 (delta-capable) |
| Tables | ~132 |
| Schedule | Daily |
| Mode | Triggered |
| Target | `acc.bronze` (same schema as Pipeline A) |

**How it works:**
- Flow A (`once=True`): Loads initial full snapshot as all-INSERT records (runs only first time)
- Flow B (ongoing): Processes daily delta CSVs with operation column (INSERT/UPDATE/DELETE)
- Both flows target the SAME table — no conflict because both use `create_auto_cdc_flow()`

---

## 5. Two-Button User Interface

### Button 1: 🔄 Sync Snapshot

- Triggers **Job 1** → runs Pipeline A + Pipeline B
- First click: creates ALL 262 tables (Pipeline A loads 130 snapshot tables, Pipeline B's ONCE flow creates 132 tables)
- Subsequent clicks: Pipeline A diffs new snapshots; Pipeline B processes pending deltas

### Button 2: ⚡ Sync CDC

- Triggers **Job 2** → runs Pipeline B only
- ONCE flow automatically skipped (already ran)
- Ongoing flows process new delta CSVs
- Pipeline A is NOT triggered

### Timeline Example

```
Day 1:  User clicks "Sync Snapshot"  → Both pipelines run → 262 tables created
Day 2:  Deltas arrive → "Sync CDC"   → Only Pipeline B → delta tables updated
Day 3:  More deltas  → "Sync CDC"    → Only Pipeline B → delta tables updated
...
Day 7:  New snapshots → "Sync Snapshot" → Pipeline A diffs + Pipeline B processes pending deltas
```

---

## 6. Key Rules & Constraints

### Table Ownership
- A table can only be owned by **ONE pipeline**
- Pipeline A owns 130 tables, Pipeline B owns 132 tables — no overlap
- Two pipelines CAN write to the same schema, just not the same table

### Method Mixing
- ❌ CANNOT combine `create_auto_cdc_from_snapshot_flow()` + `create_auto_cdc_flow()` on the SAME table
- ✅ CAN have multiple `create_auto_cdc_flow()` calls targeting the same table
- Rule: Pick ONE method per target table. Never mix.

### Why You Cannot Feed Delta CSVs to Snapshot Method
- `create_auto_cdc_from_snapshot_flow()` expects FULL data every time
- If you feed it only 5 changed rows, it thinks 995 rows were DELETED
- Result: catastrophic data loss

---

## 7. Terminology

| Term | Definition |
| --- | --- |
| **Job** | Top-level orchestration — schedules and runs tasks |
| **Task** | One unit of work inside a Job (pipeline, notebook, etc.) |
| **Pipeline** | Execution engine — runs all its flows on shared compute |
| **Flow** | Data movement logic: source → target table (many flows per pipeline) |
| **Streaming Table** | Target table type — supports incremental processing (works for batch!) |
| **ONCE flow** | A flow that runs only on the first trigger, then permanently skips |

---

## 8. Methods Comparison

| Method | Input | Use When | Delete Detection | Out-of-Order |
| --- | --- | --- | --- | --- |
| `create_auto_cdc_from_snapshot_flow()` | Full snapshots (all rows) | No CDC feed available, only periodic dumps | Automatic (missing = deleted) | N/A (point-in-time) |
| `create_auto_cdc_flow()` | Change feed (delta CSVs with operation column) | Source provides explicit change records | Explicit (operation = DELETE) | Handled automatically |
| `MERGE INTO` (manual) | Any dataframe | Custom logic needed outside pipelines | Manual condition | You handle it |

---

## 9. Snapshot Method — Advantages

- **Never lose data** — full snapshot = complete picture every time
- **Self-healing** — if something went wrong earlier, next snapshot fixes it
- **Sync at any frequency** — 1 day or 100 days gap doesn't matter
- **No sequence dependency** — just the latest full picture
- Even after 30 days without sync, one click brings tables fully up to date

---

## 10. Infrastructure Configuration

### Pipeline Settings

```json
{
  "name": "ACC_Snapshot_Pipeline",
  "catalog": "acc",
  "target": "bronze",
  "channel": "PREVIEW",
  "edition": "ADVANCED",
  "continuous": false,
  "serverless": true
}
```

### Permissions (3 GRANTs cover all 262 tables)

```sql
GRANT USE CATALOG ON CATALOG acc TO `analytics_team`;
GRANT USE SCHEMA ON SCHEMA acc.bronze TO `analytics_team`;
GRANT SELECT ON SCHEMA acc.bronze TO `analytics_team`;
```

### Environment Promotion

| Environment | Catalog | Same Code |
| --- | --- | --- |
| Dev | `acc_dev.bronze` | ✅ |
| QA | `acc_qa.bronze` | ✅ |
| Prod | `acc.bronze` | ✅ |

---

## 11. Job Configuration (API)

### Job 1: Sync Snapshot

```json
{
  "name": "ACC_Sync_Snapshot",
  "tasks": [
    {
      "task_key": "run_snapshot_pipeline",
      "pipeline_task": {"pipeline_id": "<pipeline_A_id>"}
    },
    {
      "task_key": "run_delta_pipeline",
      "pipeline_task": {"pipeline_id": "<pipeline_B_id>"},
      "depends_on": [{"task_key": "run_snapshot_pipeline"}]
    }
  ]
}
```

### Job 2: Sync CDC

```json
{
  "name": "ACC_Sync_CDC",
  "tasks": [
    {
      "task_key": "run_delta_pipeline",
      "pipeline_task": {"pipeline_id": "<pipeline_B_id>"}
    }
  ]
}
```

---

## 12. Architecture Diagram

```
                    ┌───────────────────────────────┐
                    │        YOUR APPLICATION       │
                    │                               │
                    │  [🔄 Sync Snapshot]  [⚡ Sync CDC]
                    └───────┬───────────────┬───────┘
                            │               │
                    triggers Job 1    triggers Job 2
                            │               │
                            ▼               ▼
                    ┌───────────┐    ┌───────────┐
                    │  JOB 1    │    │  JOB 2    │
                    │ (2 tasks) │    │ (1 task)  │
                    └─────┬─────┘    └─────┬─────┘
                          │                │
              ┌───────────┼────┐           │
              ▼                ▼           ▼
    ┌──────────────┐  ┌──────────────────────────┐
    │ PIPELINE A   │  │      PIPELINE B          │
    │ (13 groups)  │  │      (12 groups)         │
    │              │  │                          │
    │ Snapshot     │  │ ONCE flow: initial load  │
    │ comparison   │  │ (auto-skipped after 1st) │
    │ & merge      │  │                          │
    │              │  │ Ongoing flow: delta CSVs  │
    │              │  │ (processes new changes)   │
    └──────┬───────┘  └────────────┬─────────────┘
           │                       │
           ▼                       ▼
    ┌──────────────────────────────────────────┐
    │         acc.bronze (262 tables)          │
    │                                          │
    │  sg01_customers    (owned by Pipeline A) │
    │  sg01_addresses    (owned by Pipeline A) │
    │  ...                                     │
    │  sg14_orders       (owned by Pipeline B) │
    │  sg14_order_items  (owned by Pipeline B) │
    │  ...                                     │
    └──────────────────────────────────────────┘
```

---

## 13. When to Split Further

| Signal | Threshold | Action |
| --- | --- | --- |
| Initialization time | > 5 minutes | Split pipeline |
| Streaming tables count | > 30-40 per pipeline | Split pipeline |
| Total flows | > 250 | Split pipeline |
| Driver unresponsive | Query durations increasing | Split pipeline |

---

## 14. Edge Cases

| Scenario | What Happens |
| --- | --- |
| User clicks "Sync CDC" before "Sync Snapshot" | Pipeline B's ONCE flow creates 132 tables. Pipeline A tables don't exist yet (need separate snapshot click). |
| User clicks "Sync Snapshot" twice in a row | Pipeline A diffs (if new snapshot exists). Pipeline B: ONCE skipped, ongoing processes pending deltas. |
| No new delta CSVs exist when "Sync CDC" clicked | Pipeline B triggers → ongoing flows find no new files → fast exit → no changes. |
| Pipeline A fails but Pipeline B succeeds | 130 snapshot tables not updated, but 132 delta tables ARE updated (partial success). |
| 30 days without any sync, then "Sync Snapshot" | Pipeline A: diffs snapshots, catches up all 30 days of changes in one shot. Pipeline B: processes all accumulated deltas. |

---

## 15. Normal Catalog vs "Shared" Catalog

### Answer: There is NO special "shared catalog" type. All catalogs are the same object.

A catalog becomes "shared" purely by **granting permissions** to multiple users/groups. There is no checkbox, no special creation command, no different type.

```
"Normal" catalog:    CREATE CATALOG acc;  (only creator can access)
"Shared" catalog:    CREATE CATALOG acc;  + GRANT USE CATALOG TO multiple_teams;
                     ↑ Same command! The difference is only WHO has access.
```

### What the user must do to "share" a catalog:

```sql
-- Step 1: Create catalog (same for "normal" or "shared")
CREATE CATALOG acc;

-- Step 2: Make it "shared" by granting access to teams
GRANT USE CATALOG ON CATALOG acc TO `team_a`;
GRANT USE CATALOG ON CATALOG acc TO `team_b`;
GRANT USE CATALOG ON CATALOG acc TO `team_c`;

-- Step 3: Grant schema access
GRANT USE SCHEMA ON SCHEMA acc.bronze TO `team_a`;
GRANT USE SCHEMA ON SCHEMA acc.bronze TO `team_b`;

-- Step 4: Grant data access
GRANT SELECT ON SCHEMA acc.bronze TO `team_a`;      -- read-only
GRANT ALL PRIVILEGES ON SCHEMA acc.bronze TO `team_b`;  -- full access
```

### Comparison:

| Aspect | "Normal" Catalog | "Shared" Catalog |
| --- | --- | --- |
| Creation command | `CREATE CATALOG x` | `CREATE CATALOG x` (SAME!) |
| Special flag/setting | None | None |
| Who can access | Only owner | Multiple users/groups (via GRANTs) |
| Technical difference | None | None — it's purely about permissions |
| Workspace binding | Optional | Optional |

### Schema is the same concept:

```sql
-- "Normal" schema (only owner):
CREATE SCHEMA acc.bronze;

-- "Shared" schema (multiple teams):
CREATE SCHEMA acc.bronze;
GRANT USE SCHEMA ON SCHEMA acc.bronze TO `analytics_team`;
GRANT SELECT ON SCHEMA acc.bronze TO `analytics_team`;
```

### For your ACC pipeline:
- Create ONE catalog `acc`
- Grant permissions to whoever needs it
- That makes it "shared" — no extra steps needed

---

## 16. Schema: Evolution vs Inference (in Auto Loader)

### These are TWO DIFFERENT concepts:

| Concept | What It Does | When It Happens |
| --- | --- | --- |
| **Schema Inference** | GUESSES column data types from sample data | First time reading files |
| **Schema Evolution** | ADDS new columns when source structure changes over time | Ongoing, as new files arrive |

### Schema Inference (one-time guess):

```
Your CSV arrives:   id, name, age, salary
                    1, Alice, 30, 50000

Schema Inference decides:
  inferColumnTypes = false (default):  ALL columns → STRING
  inferColumnTypes = true:             id→INT, name→STRING, age→INT, salary→DOUBLE
```

### Schema Evolution (ongoing adaptation):

```
Day 1 CSV:    id, name, age
Day 30 CSV:   id, name, age, department, hire_date    ← 2 NEW columns!

Without evolution: Pipeline FAILS or ignores new columns
With evolution:    Pipeline auto-adds "department" and "hire_date" to table schema
```

### Schema Evolution Modes (Auto Loader):

| Mode | Behavior | Use When |
| --- | --- | --- |
| `addNewColumns` (DEFAULT) | Stops stream, adds column, restarts | Source may add columns; you want them captured |
| `rescue` | Never evolves schema, puts unknown columns in `_rescued_data` | You want strict schema, but don't want to lose data |
| `failOnNewColumns` | Stops permanently until you fix | You want manual approval of schema changes |
| `none` | Ignores new columns silently | You don't care about new columns |

### For YOUR ACC Pipeline — Use Schema EVOLUTION (not just inference):

```python
# In your Auto Loader configuration:
spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "csv")
    .option("header", "true")
    .option("cloudFiles.inferColumnTypes", "true")        # ← Inference (first time)
    .option("cloudFiles.schemaEvolutionMode", "addNewColumns")  # ← Evolution (ongoing)
    .load(path)
```

### Why Evolution matters for ACC:

```
Scenario: ACC source system adds a new field "priority" to orders table

Without Schema Evolution:
  → Pipeline fails or loses the new column
  → Manual intervention needed
  → Downtime

With Schema Evolution:
  → Auto Loader detects new column "priority"
  → Adds it to the streaming table schema automatically
  → Pipeline continues processing
  → No manual work needed
```

### Key difference from "inferred" schema:

| | Schema Inference | Schema Evolution |
| --- | --- | --- |
| When | First time only | Every time new files arrive |
| Purpose | Guess initial column types | Handle structural changes over time |
| What it does | Determines INT vs STRING vs DOUBLE | Adds/widens columns |
| In Lakeflow Pipelines | Managed automatically | Managed automatically |
| Your control | `cloudFiles.inferColumnTypes` | `cloudFiles.schemaEvolutionMode` |

### Important for your architecture:
- In **Lakeflow Pipelines**, schema location and checkpoints are managed AUTOMATICALLY
- You don't need to set `schemaLocation` manually
- Schema evolution mode `addNewColumns` is recommended for ACC (source may change over time)
- Pipeline auto-restarts after schema change (with Lakeflow Jobs scheduling)

---

## 17. Next Steps

1. Define the complete SERVICE_GROUPS_CONFIG with all 25 group names, tables, and keys
2. Create Pipeline A and Pipeline B in Databricks (Serverless, Advanced edition)
3. Upload initial full snapshot CSVs to `/Volumes/acc/raw/snapshots/`
4. Click "Sync Snapshot" to create all 262 tables
5. Set up daily delta CSV delivery to `/Volumes/acc/raw/deltas/`
6. Schedule "Sync CDC" daily via Job 2
7. Schedule "Sync Snapshot" weekly via Job 1 (or trigger on-demand)

---

*End of document*