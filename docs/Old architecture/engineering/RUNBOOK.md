# Engineering Runbook

**Audience:** engineers joining the project — what runs where, in what order, and what to watch out for.

**Companion docs:** [PLATFORM.md](../architecture/PLATFORM.md) (strategy & ingestion lock), [OVERVIEW.md](../architecture/OVERVIEW.md) (diagrams), [ACC_APIS.md](../reference/ACC_APIS.md) (API constraints & §1.1 matrix), [TESTING.md](TESTING.md) (test steps).

---

## 0. Mental model

Three roles: **SaaS control plane** (Flask) orchestrates; **customer Databricks data plane** downloads and pipelines; **Autodesk ACC** is the source.

- SaaS holds tokens, orchestrates, presents UI. No customer data on the host when `ENABLE_NOTEBOOK_DOWNLOAD=true`.
- Customer Databricks does heavy lifting — CSV download, DLT, Delta writes.
- We pull from ACC; we don't push.

Full diagrams: [OVERVIEW.md](../architecture/OVERVIEW.md).

---

## 1. Repository map

| Path | Role |
|---|---|
| [acc-connector/app.py](../../acc-connector/app.py) | Flask web app — routes, OAuth callbacks, wizard UI |
| [acc-connector/backend/bootstrap.py](../../acc-connector/backend/bootstrap.py) | 12-step workspace provisioning (one-time per customer) |
| [acc-connector/backend/sync_service.py](../../acc-connector/backend/sync_service.py) | Sync orchestrator — `snapshot` and `cdc` modes |
| [acc-connector/backend/acc_client.py](../../acc-connector/backend/acc_client.py) | APS + Data Connector REST; token refresh |
| [acc-connector/backend/databricks_client.py](../../acc-connector/backend/databricks_client.py) | Databricks REST (Jobs, Pipelines, Files, SQL) |
| [acc-connector/backend/state_store.py](../../acc-connector/backend/state_store.py) | Fernet-encrypted SQLite — tokens, watermarks, sync_runs |
| [acc-connector/backend/zerobus_status.py](../../acc-connector/backend/zerobus_status.py) | Diagnostic only, gated by `ENABLE_ZEROBUS` |
| [acc-connector/notebooks/bulk_downloader.py](../../acc-connector/notebooks/bulk_downloader.py) | Customer-side downloader — manifest, `_SUCCESS`, cleanup on failure |
| [acc-connector/notebooks/auto_cdc_pipeline.py](../../acc-connector/notebooks/auto_cdc_pipeline.py) | DLT — AUTO CDC FROM SNAPSHOT → SCD2 Bronze |
| [acc-connector/notebooks/auto_cdc_cdc_pipeline.py](../../acc-connector/notebooks/auto_cdc_cdc_pipeline.py) | DLT — AUTO CDC for CDC-beta exports |
| [acc-connector/config/pk_config_template.json](../../acc-connector/config/pk_config_template.json) | Operator PK overrides per `(service_group, table)` |
| [acc-connector/connector.db](../../acc-connector/connector.db) | Runtime SQLite. **Encrypted columns**. |
| [acc-connector/.env](../../acc-connector/.env) | Real config + secrets. Never commit. |

Auth model and phasing rules: [PLATFORM.md](../architecture/PLATFORM.md).

---

## 2. End-to-end chronological sequence

### Phase A — engineer setup (once per deployment)

| # | What happens | Where | Output |
|---|---|---|---|
| A1 | Register APS app at `aps.autodesk.com/myapps` | manual | APS `client_id`, `client_secret` |
| A2 | Register Databricks OAuth app in workspace Settings → Developer | manual | DBX `client_id`, `client_secret` |
| A3 | Populate `acc-connector/.env` | local | `.env` ready |
| A4 | `start.bat` (or `python app.py`) | local | Flask on `:8000` |

### Phase B — customer onboarding (once per customer account)

| # | What happens | Where | Output / state |
|---|---|---|---|
| B1 | User visits app URL | browser | Step 1 panel |
| B2 | *Connect ACC* → 3-legged APS OAuth | `app.py:/callback` | `acc_tokens`, `session['user_id']` |
| B3 | User picks hub + project | `/hubs`, `/projects` | `acc_config` |
| B4 | *Connect Databricks* → OIDC OAuth | `/databricks/callback` | `dbx_tokens` |
| B5 | User picks UC catalog | `/databricks/catalogs` | UI selection |
| B6 | *Run workspace provisioning* | `bootstrap.run_bootstrap()` | Steps B6.1–B6.11 below |

Bootstrap step detail and test procedure: [TESTING.md §3](TESTING.md#3-test-a--bootstrap-both-pipelines).

| # | Step | Output |
|---|---|---|
| B6.1 | Validate workspace access | 200 from clusters API |
| B6.2 | Detect SQL Warehouse | `warehouse_id` |
| B6.3–4 | Validate UC / verify catalog | — |
| B6.5–6 | CREATE SCHEMA bronze, CREATE VOLUME | bronze schema, volume |
| B6.7 | Files API smoke-test | — |
| B6.8 | CREATE `_meta_*` tables | registry, status, schema_versions |
| B6.9 | Upload notebooks to `/Users/<email>/acc/v1/` | notebooks in workspace |
| B6.9b | Bulk-downloader Job | `download_job_id` |
| B6.10 | Snapshot + CDC pipelines + workflows | pipeline/workflow IDs |
| B6.11 | Persist IDs in `bootstrap_state` | SQLite |

### Phase C — first sync (manual full export)

| # | What happens | Where |
|---|---|---|
| C1 | *One Time Full Export* | `app.py:/sync` → `sync_service.run_sync` |
| C2 | Rate-limit guard (24h) | `_assert_manual_full_rate_limit` |
| C3 | Pipeline idle guard | `_assert_bronze_pipelines_idle` |
| C4–C6 | DC create → poll job | `acc_client.py` |
| C7 | Notebook path **or** Flask upload | `_phase2_via_notebook` / `_phase2_in_flask` |
| C8–C10 | (notebook) Download job, cleanup on failure | `bulk_downloader.py` |
| C11–C12 | (Flask) PK registry seed from zip | `sync_service.py` |
| C13–C15 | Trigger snapshot pipeline → SCD2 Bronze | `auto_cdc_pipeline.py` |
| C16–C17 | Watermark + `sync_runs` complete | `state_store` |

### Phase D — recurring CDC sync (daily toggle)

| # | What happens | Where |
|---|---|---|
| D1 | Enable daily CDC on dashboard | `/sync/cdc/enable` |
| D2 | `set_daily_cdc_enabled(true)` | `state_store.py` |
| D3 | Scheduler calls `run_cdc_sync` | `app.py` timer |
| D4–D8 | CDC export → `data_connector_cdc/` → CDC pipeline | `sync_service.py` |

Phasing gates (Phase 1 before Phase 2): [PLATFORM.md §4](../architecture/PLATFORM.md#4-ingestion-phasing-rollout).

---

## 3. Component deep-dives

### 3.1 `app.py` — Flask control plane

- Routes, OAuth callbacks, wizard UI (inline HTML), background-thread launchers.
- **PKCE on Databricks OAuth.** ACC: auth-code + refresh token.
- **Session:** 24-hour sliding idle. Signed cookie via `SECRET_KEY`.
- UI polls `/sync/status` during sync.

### 3.2 `bootstrap.py` — workspace provisioning

- 12 steps, **idempotent**. Progress via `/bootstrap/status`.
- Three `_meta_*` tables — see [SCHEMA.md](../architecture/SCHEMA.md).
- Provisions: 1 download job + 2 DLT pipelines + 2 workflows.

### 3.3 `bulk_downloader.py` — customer-side downloader

- Input: `manifest_path` in UC Volume.
- Streams signed URLs to volume paths; 8 parallel workers; 64 KB chunks.
- 3-attempt backoff; removes `vol_dir` on failure; writes `_SUCCESS`.

### 3.4 `auto_cdc_pipeline.py` — snapshot DLT pipeline

- `dlt.create_auto_cdc_from_snapshot_flow` → SCD2 Bronze.
- PK from `_meta_bronze_pk_registry`. Hard + soft deletes via SCD2.

### 3.5 `sync_service.py` — orchestrator

- `run_sync` → snapshot; `run_cdc_sync` → CDC.
- Shared `_run_dc_export`; guards: idle, rate-limit, daily-CDC flag.

### 3.6 `state_store.py` — encrypted SQLite

| Table | Purpose |
|---|---|
| `acc_tokens` | Encrypted ACC OAuth tokens |
| `acc_config` | Hub / project per user |
| `dbx_tokens` | Encrypted Databricks OAuth tokens |
| `bootstrap_state` | Pipeline/job/workflow IDs |
| `watermarks` | Last sync timestamp per `(user, project, data_type)` |
| `sync_runs` | Sync run state machine |

Fernet encryption keyed from `SECRET_KEY`.

---

## 4. Loopholes & known risks — with recommendations

### 4.1 Five stale service groups in `DC_ALL_SERVICE_GROUPS`

**Risk:** `clashes`, `estimates`, `issuesbim360`, `packages`, `takeoff` may be stale or empty.

**Recommendation:** Monitor zero CSV count; remove when Autodesk confirms deprecation.

### 4.2 3-legged user OAuth, SSA not yet primary in code

**Risk:** [PLATFORM.md](../architecture/PLATFORM.md) targets SSA for sync; code may still use 3-legged refresh tokens. 90-day inactivity breaks unattended sync.

**Recommendation:** Implement SSA whitelist UX; prefer SSA in `get_valid_token` when verified.

### 4.3 No early warning on 90-day refresh-token expiry

**Recommendation:** Proactive token health check; dashboard banner; T-14/T-7/T-1 emails.

### 4.4 Data Connector quota (24 jobs / 24h per account)

**Recommendation:** Track `dc_job_count_24h`; refuse sync near limit; bundle CDC groups in one job.

### 4.5 CDC service groups are beta

**Recommendation:** CDC is additive — log + skip failed groups; don't fail entire run.

### 4.6 PK config template drift

**Recommendation:** CI check template vs `schemas/*.json`; operator review on diff.

### 4.7 Activities not implemented

**Recommendation:** Separate sync mode if added — see [PLATFORM.md §5](../architecture/PLATFORM.md#5-service-groups--deployment-table).

### 4.8 Manual rate limit blocks failed retries

**Recommendation:** Rate-limit on **successful** runs only.

### 4.9 No automated tests

**Recommendation:** Smoke tests for OAuth, bootstrap idempotency, snapshot path — see [TESTING.md](TESTING.md).

### 4.10 Legacy SQLite columns

**Recommendation:** Document as orphan; drop in future migration.

### 4.11 Background-thread errors not retried

**Recommendation:** Soft retry on transient 429/5xx.

### 4.12 Inline HTML — XSS surface

**Recommendation:** Jinja templates with autoescape for PWAF.

Per-service sync frequency research: [ACC_APIS.md §1.1](../reference/ACC_APIS.md#11-consolidated-service-group-capability--sync-frequency).

---

## 5. Quick reference

### 5.1 Environment variables

| Var | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes | Flask session + Fernet |
| `APS_CLIENT_ID` / `APS_CLIENT_SECRET` | Yes | Autodesk OAuth |
| `APS_REDIRECT_URI` | Yes | e.g. `http://localhost:8000/callback` |
| `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` | Yes | Databricks OIDC |
| `DATABRICKS_REDIRECT_URI` | Yes | e.g. `http://localhost:8000/databricks/callback` |
| `PORT` | Optional | Default `8000` |
| `ENABLE_NOTEBOOK_DOWNLOAD` | Optional | Customer-side CSV download |
| `ENABLE_ZEROBUS` | Optional | Diagnostics only |
| `DC_JOB_APPEAR_WAIT` | Optional | Default `1800` |

### 5.2 Databricks artifacts per customer

| Name | Type |
|---|---|
| `<catalog>.bronze` | Schema |
| `<catalog>.bronze.acc_bronze_volume` | Volume |
| `<catalog>.bronze._meta_bronze_*` | Meta Delta tables (3) |
| `/Users/<email>/acc/v1/auto_cdc_pipeline` | Notebook |
| `/Users/<email>/acc/v1/bulk_downloader` | Notebook |
| `CCTech_FormaConnector_DCDownloader` | Job |
| `CCTech_ACCConnector_Bronze` | DLT Pipeline |
| `CCTech_ACCConnector_Bronze_CDC` | DLT Pipeline |
| `CCTech_ACCConnector_Snapshot_Sync` / `_CDC_Sync` | Workflows |
| `<catalog>.bronze.<table>` | Bronze Delta (many) |

### 5.3 Sync-run states

`pending` → `running` → `dc_job_submitted` → … → `complete` | `failed`

Full chain: see prior TEAM_KT §5.3 — `dc_waiting_for_job` → `dc_job_running` → `uploading_files` → `downloader_job_triggered` → `uploading_files_complete` → `bronze_written` → `bronze_job_triggered` → `bronze_job_running` → `complete`.

### 5.4 Local commands

```powershell
cd acc-connector
.\start.bat          # or: python app.py
.\stop.bat
sqlite3 connector.db "SELECT user_id, state FROM sync_runs ORDER BY run_id DESC LIMIT 10"
```

---

## 6. Onboarding checklist

1. Read this doc (~30 min).
2. Read [PLATFORM.md](../architecture/PLATFORM.md) (15 min).
3. Skim [ACC_APIS.md §1.1](../reference/ACC_APIS.md#11-consolidated-service-group-capability--sync-frequency) (20 min).
4. Run locally: `start.bat`, complete wizard with APS + Databricks credentials.
5. Watch one full sync — trace `sync_runs` states.
6. Run [TESTING.md](TESTING.md) scenarios.
7. Pick one item from §4; draft a fix proposal.

---

## 7. Where to add things

| If you need to… | Edit |
|---|---|
| Add ACC service group | `acc_client.py:DC_ALL_SERVICE_GROUPS` + `pk_config_template.json` |
| Add bootstrap step | `bootstrap.py` + `_meta_*` if needed |
| Add sync_runs state | `db.update_sync_run(run_id, 'new_state')` |
| Add feature flag | `.env.example` + consumer module |
| Add Databricks resource | `databricks_client.py` + `bootstrap.py` |
| Add UI panel | `app.py` HTML (or future `templates/`) |
| Add sync mode | extend `_run_dc_export` in `sync_service.py` |

---

**Last updated:** 2026-06-18.
