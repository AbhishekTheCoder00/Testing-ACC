# ACC Connector v2 — Session State

> Last updated: May 27, 2026 — **Single ingestion path (Data Connector bulk only).**
> Purpose: Resume context for new AI sessions. Read this file first before doing anything.

---

## Next Session Prompt (copy-paste this exactly)

> "Continue from previous session. Read SESSION_STATE.md at C:\Users\Prabal Singh\Desktop\POC final\ for full context.
>
> Current state: ACC Connector v2 has **one live ingestion path**:
>   **Data Connector bulk** — POST DC request → poll job → land CSVs in UC Volume → seed PK registry from `autodesk_data_extract.zip` → trigger Bronze **AUTO CDC FROM SNAPSHOT** pipeline (`notebooks/auto_cdc_pipeline.py`).
>
> No REST entity-ingest code in the repo (ingestors, flatteners, DirectDelta, `fetch_data()` removed). **Zerobus:** `backend/zerobus_status.py` only — prerequisite diagnostics when `ENABLE_ZEROBUS=true` (`GET /sync/zerobus-status`); not used by `run_sync()`.
>
> Ingestion locked: [PLATFORM.md](../architecture/PLATFORM.md). REST research: [ACC_APIS.md §1.1](../reference/ACC_APIS.md#11-consolidated-service-group-capability--sync-frequency).
>
> See [SCHEMA.md](../architecture/SCHEMA.md) for PK registry + AUTO CDC. See [RUNBOOK.md](../engineering/RUNBOOK.md) for flows and module reference."

---

## Environment

| Key | Value |
|-----|-------|
| Project folder | C:\Users\Prabal Singh\Desktop\POC final\acc-connector\ |
| Local app URL | http://localhost:8000 |
| Python version | 3.11 |
| **Databricks Workspace** | https://dbc-a8cd4672-fe3e.cloud.databricks.com (AWS — friend's workspace) |
| Databricks Account ID | (visible in error messages — recorded in .env via OAuth flow) |
| APS App Callback URL | http://localhost:8000/callback |
| Databricks OAuth Callback | http://localhost:8000/databricks/callback |
| State DB | connector.db (auto-created in acc-connector/ on first run) |
| .env location | C:\Users\Prabal Singh\Desktop\POC final\acc-connector\.env |
| start.bat / stop.bat | acc-connector\ — used to manage the Flask app |

---

## Current .env Values

| Variable | Purpose |
|----------|---------|
| SECRET_KEY | Flask session encryption |
| APS_CLIENT_ID | Autodesk APS app — user-facing OAuth |
| APS_CLIENT_SECRET | Autodesk APS app secret |
| APS_REDIRECT_URI | http://localhost:8000/callback |
| DATABRICKS_WORKSPACE_URL | https://dbc-a8cd4672-fe3e.cloud.databricks.com |
| DATABRICKS_CLIENT_ID | afc0f327-b38a-401b-9ae5-ec8e8ea0f042 (Databricks Service Principal — U2M OAuth) |
| DATABRICKS_CLIENT_SECRET | (same SP secret) |
| DATABRICKS_REDIRECT_URI | http://localhost:8000/databricks/callback |
| PORT | 8000 |
| ENABLE_ZEROBUS | `false` (default) — Zerobus diagnostic route only; no live sync path |
| ENABLE_NOTEBOOK_DOWNLOAD | optional — DC download via `bulk_downloader` job instead of Flask |

> NOTE: The DATABRICKS_CLIENT_ID/SECRET pair is the Databricks Service Principal for 3-legged user OAuth. M2M may be needed later for a Zerobus writer (see docs only today).

---

## Architecture (as of May 2026)

### Single sync path — Data Connector bulk

| Stage | What happens |
|-------|----------------|
| **Phase 1** | `acc_client.dc_create_request` → poll job → all ~25 `serviceGroups` in one export |
| **Phase 2** | Download CSVs (+ `autodesk_data_extract.zip`) → UC Volume `…/data_connector/{project}/{timestamp}/` |
| **Phase 2.5** | Parse zip → MERGE PK rows into `<catalog>.bronze._meta_bronze_pk_registry` |
| **Phase 3** | Trigger `auto_cdc_pipeline.py` with `acc.dc_snapshot_path` (this run only) → Bronze SCD2 via AUTO CDC FROM SNAPSHOT. Manual sync blocked while any Bronze pipeline update is active. Technique map: [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md) |

No ACC entity JSON is persisted on the connector host except encrypted tokens in SQLite.

### Removed (clean-up release) — do not assume in code

| Removed | Was |
|---------|-----|
| `backend/ingestors/` | `IssuesIngestor`, `BaseIngestor`, registry |
| `backend/flatteners/` | `flatten_issue`, per-entity Bronze DDL |
| `_run_sync_realtime()` | REST → DirectDelta / Zerobus writer |
| Step 3 radio (bulk vs realtime) | UI mode switch |
| `sync_mode` SQLite usage | Per-user realtime entity selection |

### Optional: Zerobus diagnostics (`ENABLE_ZEROBUS=false` by default)

| Module | Status |
|--------|--------|
| `backend/zerobus_status.py` | Catalog storage + SP grant checks — `GET /sync/zerobus-status` only when flag is true |

### Schema authority

- **Runtime:** `autodesk_data_extract.zip` → `schemas/schema.json` (per tenant, per run)
- **Offline docs:** `acc-connector/schemas/*.json` — never read at runtime ([SCHEMA.md](../architecture/SCHEMA.md))

---

## Current Status

| Flow | Status |
|------|--------|
| ACC OAuth | WORKING — use Autodesk identity provisioned in tenant |
| Hub / Project dropdown | WORKING |
| Databricks OAuth | WORKING |
| Bootstrap (12 steps) | WORKING — catalog, bronze schema, volume, meta tables, notebooks, AUTO CDC pipeline |
| Bulk sync (Data Connector) | **WORKING — only sync path** |
| REST realtime / Issues ingestor | **REMOVED** — not in repo |
| Zerobus diagnostics | **OPTIONAL** — `ENABLE_ZEROBUS=true` enables `/sync/zerobus-status` only |
| Per-service REST ingest | **NOT IN CODE** — [ACC_APIS.md](../reference/ACC_APIS.md), [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md) |
| Silver layer | Separate from bulk AUTO CDC path — not covered here |

---

## Open Blockers / Awaiting Inputs

| # | Blocker | Owner | Status |
|---|---|---|---|
| 1 | Autodesk login / tenant provisioning for test account | User | Use identity with ACC access |
| 2 | Two-pipeline CDC implementation | Engineering | [PLATFORM.md](../architecture/PLATFORM.md) checklist |
| 3 | Future REST or Zerobus writer | Engineering | [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md) |

---

## Deferred: Zerobus writer (optional, not blocking bulk sync)

> Bulk sync uses Volume + AUTO CDC FROM SNAPSHOT. REST/ingestor design stays in markdown only.

| # | File | Change | Status |
|---|---|---|---|
| 1 | New writer module + `databricks-sdk` | gRPC Zerobus client when product requires it | NOT STARTED |
| 2 | `app.py` | `/sync/zerobus-status` diagnostics | **DONE** behind `ENABLE_ZEROBUS` |

`backend/databricks_client.py` — `User-Agent: CCTech_ACCConnector/2.0` on REST calls (**DONE**).

---

## How to Restart the App

```powershell
cd "C:\Users\Prabal Singh\Desktop\POC final\acc-connector"
.\stop.bat
.\start.bat
```

App boots at http://localhost:8000.

For full reset (wipes SQLite state):

```powershell
cd "C:\Users\Prabal Singh\Desktop\POC final\acc-connector"
.\stop.bat
Remove-Item connector.db -Force -ErrorAction SilentlyContinue
.\start.bat
```

Then hard refresh browser (Ctrl+Shift+R).

---

## Code Files (Current State)

```
C:\Users\Prabal Singh\Desktop\POC final\
├── acc-connector\
│   ├── backend\
│   │   ├── acc_client.py             ← APS OAuth + Data Connector API
│   │   ├── bootstrap.py              ← 12-step UC provisioning + pipeline create
│   │   ├── databricks_client.py      ← Databricks REST: SQL, UC, Files API, pipelines
│   │   ├── state_store.py            ← SQLite + Fernet (tokens, watermarks, sync_runs)
│   │   ├── sync_service.py           ← DC bulk only: run_sync → _run_sync_bulk
│   │   └── zerobus_status.py         ← Optional diagnostics (ENABLE_ZEROBUS)
│   ├── notebooks\
│   │   ├── auto_cdc_pipeline.py      ← Bronze AUTO CDC FROM SNAPSHOT (live)
│   │   └── bulk_downloader.py        ← Optional DC download in workspace
│   ├── schemas\                      ← Offline DC schema reference (not runtime)
│   ├── app.py                        ← Flask UI; POST /sync starts bulk only
│   ├── connector.db
│   └── ...
├── docs/                             ← documentation (see map below)
└── README.md                         ← doc hub
```

---

## Documentation map

| Doc | Path |
|-----|------|
| Platform (strategy + ingestion) | [docs/architecture/PLATFORM.md](../architecture/PLATFORM.md) |
| Runbook | [docs/engineering/RUNBOOK.md](../engineering/RUNBOOK.md) |
| Schema evolution | [docs/architecture/SCHEMA.md](../architecture/SCHEMA.md) |
| ACC API research (§1.1 matrix) | [docs/reference/ACC_APIS.md](../reference/ACC_APIS.md) |
| Future REST | [docs/reference/REST_FUTURE.md](../reference/REST_FUTURE.md) |
| Testing | [docs/engineering/TESTING.md](../engineering/TESTING.md) |
| Launch checklist | [docs/operations/LAUNCH.md](../operations/LAUNCH.md) |

---

## Historical note (May 13–15, 2026)

A short-lived build added REST realtime (ingestors, flatteners, DirectDelta, Step 3 radio). That stack was **removed** in favor of DC-only bulk + AUTO CDC FROM SNAPSHOT. Do not search the repo for `backend/ingestors/` or `backend/flatteners/` — they are gone.
