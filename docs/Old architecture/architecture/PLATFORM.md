# Platform — Strategy, Auth & Ingestion

**Status:** Locked architectural direction for v1 partner launch.  
**Last reviewed:** 2026-06-09.

Canonical doc for product framing, authentication, ingestion lock, rollout phasing, and locked decisions.  
For REST/webhook/sync-frequency research per service group, see [ACC_APIS.md §1.1](../reference/ACC_APIS.md#11-consolidated-service-group-capability--sync-frequency).  
For schema/PK behavior, see [SCHEMA.md](SCHEMA.md). For CDC technique mapping, see [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md).

---

## 1. Product framing

- **Brand:** *Forma → Databricks*, where "Forma" is Autodesk's umbrella for the AEC industry cloud.
- **Phase 1 data scope:** ACC (Autodesk Construction Cloud). Other Autodesk products (Build, BIM 360, Spacemaker-Forma) come later under the same connector.
- **Customer model:** enterprise BYOD (Bring Your Own Databricks). Customers connect their own Databricks workspace; data flows ACC → customer's Unity Catalog. **SaaS is the control plane; data plane runs in the customer's workspace.**
- **Storage shape:** SCD Type 2 Bronze Delta tables built from periodic Data Connector snapshots, plus CDC-beta deltas on overlapping domains.

Diagram: [OVERVIEW.md](OVERVIEW.md).

---

## 2. Authentication model

### 2.1 Identity types available in ACC

| Identity | OAuth grant | ACC data endpoints | Reviews API | Token longevity | Notes |
|---|---|---|---|---|---|
| **3-legged user** | `authorization_code` + PKCE + `refresh_token` | ✓ | ✓ | Access 1h; refresh expires after ~90 days inactivity | User must consent once; refresh-token model required for headless ops |
| **SSA — Service-to-Service Account** | `client_credentials` against an APS app whitelisted at the customer's ACC Account Admin → Custom Integrations | ✓ | ✗ (Reviews requires user context) | Access 1h; mint a fresh one any time | The only "M2M-like" path for ACC data; effective Executive Overview at account scope |
| **Pure 2-legged** (`client_credentials`, no SSA whitelist) | `client_credentials` | ✗ for project data | ✗ | n/a | Works only for hub/project enumeration; useless for sync |

**Pure M2M for ACC data does not exist.** What this project calls "M2M sync" means **SSA**.

### 2.2 Locked decision: SSA primary, 3-legged carved out

| Operation | Identity used | Why |
|---|---|---|
| Onboarding handshake | 3-legged user OAuth (account admin) | Required to verify the human is an account admin and to authorize the SSA whitelist step |
| Phase 1 initial full export | **SSA** | May run for hours; cannot be bound to a user session |
| Phase 2 manual CDC pull | **SSA** | Same reason; also future auto-sync uses the same path |
| Phase 3 auto CDC sync | **SSA** | No user in the loop |
| Manual snapshot refresh from UI | **SSA** | Trigger is human, work is headless |
| Reviews API ingestion | **3-legged user refresh token** (carve-out) | Autodesk explicitly rejects SSA on Reviews endpoints |
| Hub/project enumeration | 2-legged (`client_credentials`) | App-only token sufficient |

### 2.3 SSA registration flow (one-time per customer account)

1. Customer admin signs in via 3-legged OAuth at `/callback`.
2. SaaS verifies the user has the Account Admin role.
3. SaaS surfaces a step-by-step UI: *"Authorize CCTech Forma Connector at your ACC account level"* — out-of-band at **Account Admin → Custom Integrations**.
4. SaaS provides *"Verify whitelist"*. On click, mint app-only token via `client_credentials` and call a low-cost Data Connector list endpoint to confirm SSA returns project data.
5. Once verified, record `ssa_verified_at` in `bootstrap_state` and unlock Phase 1.
6. Store the onboarding user's encrypted refresh token under `acc_tokens` — Reviews-fallback credential.

### 2.4 Token storage & rotation

| Credential | Stored where | Rotation | Failure mode |
|---|---|---|---|
| APS app `client_id` / `client_secret` | `.env` on SaaS host (or secret manager in production) | Annual rotation policy; manual | Bootstrap and SSA mint fail until rotated |
| Customer-account SSA proof (verified status only) | `bootstrap_state.ssa_verified_at` | n/a | Whitelist revoked → 401 on next mint → fall back to user refresh token + alert |
| Per-user 3-legged refresh token | `acc_tokens` (Fernet-encrypted SQLite) | Sliding window 90 days inactive | T-14 / T-7 / T-1 email nudges; Reviews stops if not refreshed |

### 2.5 Why not standardize everything on user-context

- **90-day inactivity expiry** breaks headless sync.
- **User off-boarding** kills sync when refresh token is revoked.
- **Per-user audit attribution** is weak — audit signal is the sync run, not the person.
- **Permission scope is brittle** — OAuthing user may lack Executive Overview or have ACL changes.

SSA addresses all four. Reviews carve-out is a known cost, not a blocker.

---

## 3. Ingestion lock (finalized May 2026)

The connector uses **ACC Data Connector only** (no per-entity REST ingest in code). Bronze is **one SCD2 Delta table per logical ACC table**, fed by **two Lakeflow pipelines** (policy **A + B**):

| Path | User control | ACC export | Volume prefix | Databricks API | Bronze tables |
|------|--------------|------------|---------------|----------------|---------------|
| **Snapshot** | Manual **Sync** (optional; **max 1 full run / 24h**) | DC **Standard** — all service groups (~25) | `data_connector/{project}/{run}/` | **AUTO CDC FROM SNAPSHOT** | All modules with CSV + PK |
| **CDC daily** | Toggle **daily auto sync** + scheduler | DC **CDC beta** — 10 groups (`cdcissues`, …) | `data_connector_cdc/{project}/{date}/` | **AUTO CDC** (`sequence_by`, deletes) | **Same** targets as snapshot for those domains |

**Not in scope in code:** REST polling ingestors, DirectDelta SQL writer, Step 3 bulk/realtime radio. REST research: [ACC_APIS.md](../reference/ACC_APIS.md), future design: [REST_FUTURE.md](../reference/REST_FUTURE.md).

### Policy A — Non-CDC modules (Standard only)

Fourteen service groups have **no** DC CDC beta export. Updated **only** on manual full sync (FROM SNAPSHOT). Daily CDC pipeline **does not** register flows for them.

### Policy B — Overlap modules (Standard + CDC beta)

Ten groups have both Standard and CDC beta:

- **Daily (toggle ON):** CDC pipeline → `create_auto_cdc_flow` into the **same** Bronze table as Standard.
- **Manual full (optional, ≤1/day):** Snapshot pipeline → `create_auto_cdc_from_snapshot_flow` for baseline / reconcile.
- **First use:** At least one successful manual full before daily CDC is meaningful on empty targets.

Manual full is a **rate limit**, not a requirement to run every day.

### Two pipelines (deployment)

| Pipeline | Notebook | Pipeline config (per update) | Trigger |
|----------|----------|------------------------------|---------|
| **acc-bronze-snapshot** | [`acc-connector/notebooks/auto_cdc_pipeline.py`](../../acc-connector/notebooks/auto_cdc_pipeline.py) | `acc.dc_snapshot_path`, `acc.dc_project_id` | Flask `POST /sync` after upload |
| **acc-bronze-cdc** | [`acc-connector/notebooks/auto_cdc_cdc_pipeline.py`](../../acc-connector/notebooks/auto_cdc_cdc_pipeline.py) | `acc.dc_cdc_path`, `acc.dc_project_id` | Scheduler when `daily_cdc_enabled` is true |

Shared across both:

- `<catalog>.bronze._meta_bronze_pk_registry`, `_meta_bronze_table_status`, `_meta_bronze_schema_versions`
- Same `target` table name per `(schema, table)` — no `issues_cdc` sibling tables.

**Concurrency:** Do not run manual bulk while either Bronze pipeline update is in flight. Same physical tables must not be written by two pipeline runs at once.

### Connector (Flask) behavior

| Feature | Behavior |
|---------|----------|
| **Manual Sync** | DC Standard job → `data_connector/…` → snapshot pipeline. **≤1 full sync per user/project per 24h**. |
| **Manual vs pipeline** | Manual bulk sync blocked while snapshot or CDC pipeline has an active update. |
| **Daily CDC toggle** | `daily_cdc_enabled` per user/project → DC CDC-beta → `data_connector_cdc/…` → CDC pipeline. |
| **DC CDC request** | `cdcadmin`, `cdcissues`, `cdccost`, `cdcrfis`, `cdclocations`, `cdcschedule`, `cdcsubmittalsacc`, `cdcsheets`, `cdcmeetingminutes`, `cdctransmittals` |
| **DC Standard request** | `DC_ALL_SERVICE_GROUPS` (~25 groups) |
| **REST entity fetch** | **Removed** — not used |

### CDC pipeline (AUTO CDC) — design notes

| Element | Expected |
|---------|----------|
| Keys | Registry-locked PK (same as Standard) |
| Sequence | `adsk_updated_at` (confirm per table on real extract) |
| Deletes | `deleted_at IS NOT NULL` → `apply_as_deletes` |
| Source view | Read only `acc.dc_cdc_path` folder for one run |

Do **not** use `create_auto_cdc_from_snapshot_flow` on CDC delta CSVs.

### What we are not adopting

| Item | Reason |
|------|--------|
| REST ingestors / flatteners / DirectDelta | Removed; design-only in markdown |
| Separate Bronze table per CDC path | Same `target` as Standard (policy B) |
| `FROM SNAPSHOT` on CDC delta files | Wrong abstraction — use AUTO CDC |
| Mandatory daily manual full | Manual sync optional; 24h cap only |
| REPLACE WHERE on AUTO CDC targets | [Databricks limitation](https://docs.databricks.com/aws/en/ldp/flows-replace-where) |
| Zerobus in sync path | Diagnostics only (`ENABLE_ZEROBUS`); no writer in repo |

### Implementation checklist

| # | Work item | Status |
|---|-----------|--------|
| 1 | Snapshot pipeline scoped to one run folder (`acc.dc_snapshot_path`) | **Done** |
| 2 | Block manual sync while snapshot/CDC pipeline update active | **Done** |
| 3 | Flask 24h gate on manual full sync | **Done** |
| 4 | `daily_cdc_enabled` toggle + scheduler hook | **Done** |
| 5 | DC CDC job builder + `data_connector_cdc/` upload path | **Done** |
| 6 | Bootstrap: second pipeline ID in `bootstrap_state` | **Done** |
| 7 | `auto_cdc_cdc_pipeline.py` (AUTO CDC only) | **Done** |
| 8 | CDC CSV → registry table mapping | **Done** |
| 9 | POC: one overlap table — baseline snapshot → CDC → reconcile | Pending |

### Deferred (out of ingestion lock)

- Per-service REST / webhooks — [ACC_APIS.md](../reference/ACC_APIS.md), [REST_FUTURE.md](../reference/REST_FUTURE.md)
- DC `activities` serviceGroup — [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md)
- Zerobus gRPC writer (diagnostics only today)
- Silver layer transforms

---

## 4. Ingestion phasing (rollout)

### Phase 1 — Bootstrap + initial full export

**Trigger:** *"One Time Full Export (AUTO CDC FROM SNAPSHOT)"*.  
**Identity:** SSA (after whitelist verified).  
**Service groups:** 21 explicit standard names (omits stale `clashes`, `estimates`, `issuesbim360`, `packages`, `takeoff`, and meta `all`/`activities`).  
**Pipeline:** `dlt.create_auto_cdc_from_snapshot_flow` → `<catalog>.bronze.<table>` (SCD2).  
**Storage:** `/Volumes/<catalog>/bronze/acc_bronze_volume/data_connector/<project>/<ts>/`.  
**Gate:** must succeed **at least once** before Phase 2 unlocks.  
**Idempotency:** timestamped folders; on failure `bulk_downloader` removes its `vol_dir`.

### Phase 2 — Manual CDC

**Trigger:** *"Pull Changes Since Last CDC Sync"*.  
**Identity:** SSA.  
**Service groups:** 10 CDC-beta groups, bundled in one DC request.  
**Pipeline:** `CCTech_ACCConnector_Bronze_CDC`.  
**Storage:** `data_connector_cdc/<service_group>/<project>/<ts>/` — **never mixed with snapshot root**.  
**Gate:** unlocked only after Phase 1 succeeded ≥ once.

### Phase 3 — Auto-sync toggles

**Trigger:** daily CDC toggle / scheduler calling same `run_cdc_sync` as Phase 2.  
**Identity:** SSA.  
Phase 2 is the production code path; Phase 3 only changes the trigger.

---

## 5. Service groups — deployment table

Source: [`ACC_DC_SERVICE_GROUPS.csv`](../../ACC_DC_SERVICE_GROUPS.csv).  
For REST filters, webhooks, and recommended sync frequency, see [ACC_APIS.md §1.1](../reference/ACC_APIS.md#11-consolidated-service-group-capability--sync-frequency).

| Service group | Standard | CDC beta | CDC group name | Ingestion path | Notes |
|---------------|:--------:|:--------:|----------------|----------------|-------|
| admin | YES | YES | cdcadmin | Snapshot + daily CDC | Covered after CDC pull |
| cost | YES | YES | cdccost | Snapshot + daily CDC | |
| issues | YES | YES | cdcissues | Snapshot + daily CDC | |
| locations | YES | YES | cdclocations | Snapshot + daily CDC | |
| rfis | YES | YES | cdcrfis | Snapshot + daily CDC | |
| schedule | YES | YES | cdcschedule | Snapshot + daily CDC | |
| submittalsacc | YES | YES | cdcsubmittalsacc | Snapshot + daily CDC | Distinct from legacy submittals |
| sheets | YES | YES | cdcsheets | Snapshot + daily CDC | |
| meetingminutes | YES | YES | cdcmeetingminutes | Snapshot + daily CDC | |
| transmittals | YES | YES | cdctransmittals | Snapshot + daily CDC | |
| activities | YES | NO | — | Snapshot only (deferred) | Event log; separate design if added |
| assets | YES | NO | — | Snapshot only | Full re-snapshot for freshness |
| checklists | YES | NO | — | Snapshot only | |
| dailylogs | YES | NO | — | Snapshot only | |
| forms | YES | NO | — | Snapshot only | |
| iq | YES | NO | — | Snapshot only | |
| markups | YES | NO | — | Snapshot only | |
| photos | YES | NO | — | Snapshot only | |
| relationships | YES | NO | — | Snapshot only | |
| reviews | YES | NO | — | Snapshot only | Uses user-token fallback when SSA primary |
| submittals | YES | NO | — | Snapshot only | Legacy dataset |

The UI should show which domains stay fresh via CDC vs which require full snapshot rerun.

---

## 6. UI gating state machine

| State | Visible actions |
|---|---|
| ACC not connected | *Connect ACC* only |
| Databricks not connected | *Connect Databricks* only |
| Bootstrap not run | *Run Bootstrap* only |
| Bootstrap in progress | Live progress; no other actions |
| Bootstrap complete, SSA not verified | *Verify SSA Whitelist* only |
| SSA verified, no Phase 1 sync yet | *One Time Full Export* enabled; CDC **disabled** |
| Phase 1 running | *Sync running*; both action buttons disabled |
| Phase 1 succeeded ≥ once | Both action buttons enabled |
| Phase 1 last run failed | Re-enable full export; CDC disabled if no prior success |

**Critical:** bootstrap and Phase 1 are different states. Until Phase 1 succeeds once, CDC has no baseline.

---

## 7. Operational risks & mitigations

| Risk | Mitigation |
|---|---|
| Customer admin revokes SSA whitelist | Daily SSA health check; alert; fall back to user refresh token for limited domains |
| User refresh token expires (Reviews) | T-14 / T-7 / T-1 email nudge; Reviews stops if not refreshed |
| CDC service group withdrawn from beta | Log + skip; snapshot re-run captures gap |
| Data Connector quota (24 jobs/24h per account) | Bundle CDC groups; track usage; reject over-quota triggers with clear message |
| Half-written CSV poisons DLT | `bulk_downloader` removes `vol_dir` on failure |
| Mixing snapshot and CDC writes | Separate volumes, pipelines, and volume roots |
| 5 stale service groups in code list | Explicit 21-name list excludes them |
| SSA elevated permissions | Documented in customer data-handling policy |

Implementation-level risks: [RUNBOOK.md §4](../engineering/RUNBOOK.md#4-loopholes--known-risks--with-recommendations).

---

## 8. Locked decisions

1. **SSA for all sync paths except Reviews.**
2. **3-legged user OAuth for onboarding + Reviews fallback only.**
3. **Same Bronze target tables** for snapshot and CDC on overlap domains (policy B) — not separate `_changes` sibling tables in the implemented lock.
4. **Two separate volumes** (`data_connector/` vs `data_connector_cdc/`).
5. **Two separate DLT pipelines** — `CCTech_ACCConnector_Bronze` and `CCTech_ACCConnector_Bronze_CDC`.
6. **Phase 1 → Phase 2 → Phase 3 in order.**
7. **Explicit 21-service-group list, not `all`.**
8. **Activities deferred** — separate state machine if added.
9. **One APS app per SaaS deployment**, whitelisted per customer at SSA verification.
10. **Reviews 90-day clock** acceptable as v1 limitation.

**Historical (from Final Strategy, superseded by ingestion lock):** an earlier draft specified parallel Bronze surfaces (`<entity>` SCD2 vs `<entity>_changes`). The implemented lock ([§3](#3-ingestion-lock-finalized-may-2026)) uses **the same Bronze targets** on overlap domains (policy B).

---

## 9. Open questions

1. **APS app type** — eligible for SSA whitelist as-is, or parallel server-to-server app?
2. **CDC Bronze strategy** — `append_flow` vs `create_auto_cdc_flow` (default: append for history).
3. **Manual CDC quota** — at most one manual CDC pull per project per 24h?
4. **Customer-facing coverage doc** — tooltip on CDC button in UI?

---

## 10. References

- [OVERVIEW.md](OVERVIEW.md) — architecture diagrams
- [RUNBOOK.md](../engineering/RUNBOOK.md) — operational sequence
- [SCHEMA.md](SCHEMA.md) — PK registry, evolution
- [ACC_APIS.md](../reference/ACC_APIS.md) — API research, §1.1 matrix
- [CDC_TECHNIQUES.md](../reference/CDC_TECHNIQUES.md)
- [LAUNCH.md](../operations/LAUNCH.md) — production & partner checklist
- [SESSION_STATE.md](../meta/SESSION_STATE.md) — current build status
