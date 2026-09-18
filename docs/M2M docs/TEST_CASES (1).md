# Forma → Databricks Connector — Test Cases

**Version:** 1.0  
**Source:** [BUSINESS_REQUIREMENTS.md](./BUSINESS_REQUIREMENTS.md), [M2M_req 1.md](./M2M%20req%201.md)  
**Product:** Forma → Databricks Connector (`acc-connector/`)  
**Total test cases:** 89

---

## What this document covers (simple overview)

This file lists **89 test cases** for the Forma → Databricks Connector. They check that the product works correctly for **hub administrators only**, keeps each **hub isolated**, creates **only one robot (SSA) per hub**, and runs **scheduled sync without a human logged in**.

| Area | What we test | # Tests |
|------|----------------|--------|
| **Sign-in & access** | Only hub admins can use the app; others are blocked | 8 |
| **Session & hub switching** | User, hub, and connection context is tracked correctly | 7 |
| **User ↔ hub mapping** | First admin, second admin, multi-hub admin flows | 9 |
| **SSA robot setup** | One robot per hub, reuse on retry, secure storage | 12 |
| **Custom Integration whitelist** | Manual whitelist step, blocking until verified | 6 |
| **Connections** | Create, resume, share, one pipeline per catalog | 14 |
| **Sync (headless & interactive)** | M2M auth, retries, failure handling | 10 |
| **Concurrency & data rules** | No duplicate robots/connections under load | 6 |
| **End-to-end scenarios** | Full user journeys A–F from requirements | 6 |
| **Security & non-functional** | Vault, isolation, idempotency, scalability | 7 |
| **POC migration** | Production model replaces old single-user design | 4 |

**Priority legend:** **P0** = must pass for release · **P1** = important · **P2** = edge / nice-to-have

---

## 1. Authentication & Access Control

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-AUTH-01 | Hub admin signs in successfully via Autodesk OAuth | FR-01, FR-01a | P0 |
| TC-AUTH-02 | Project admin (non–hub-admin) is denied access after OAuth | FR-01a, BR-12, BO-08 | P0 |
| TC-AUTH-03 | Viewer-only user is denied access after OAuth | FR-01a, BR-12 | P0 |
| TC-AUTH-04 | User with no hub admin role on any hub sees clear access-denied message | FR-01a, BO-08 | P0 |
| TC-AUTH-05 | Hub admin of Hub A cannot access Hub B data without mapping on Hub B | NFR-02, BR-01 | P0 |
| TC-AUTH-06 | Session expires and user must re-authenticate | FR-01 | P1 |
| TC-AUTH-07 | OAuth callback handles invalid/expired authorization code gracefully | FR-01 | P1 |
| TC-AUTH-08 | Only hub_admin role stored in tenant_users — not project_admin or viewer | FR-07 | P0 |

### TC-AUTH-01 — Hub admin signs in successfully via Autodesk OAuth

**Preconditions:** User is ACC/Forma hub administrator for at least one hub.

**Steps:**
1. Open connector login page.
2. Complete Autodesk OAuth (3-legged) flow.
3. System verifies hub admin role.

**Expected:** User lands in app (hub picker or dashboard). Session contains `aps_user_id`.

---

### TC-AUTH-02 — Project admin (non–hub-admin) is denied access after OAuth

**Preconditions:** User is project admin only — not hub admin on any hub.

**Steps:**
1. Sign in via Autodesk OAuth.
2. System evaluates hub admin membership.

**Expected:** Access denied with clear message. No tenant or connection data created.

---

### TC-AUTH-03 — Viewer-only user is denied access after OAuth

**Preconditions:** User has viewer role only on ACC projects.

**Steps:** Same as TC-AUTH-02.

**Expected:** Access denied. No product session created.

---

### TC-AUTH-04 — User with no hub admin role on any hub sees clear access-denied message

**Preconditions:** Valid Autodesk account with zero hub admin privileges.

**Steps:** Complete OAuth.

**Expected:** Message explains product is for hub administrators only. No partial onboarding state.

---

### TC-AUTH-05 — Hub admin of Hub A cannot access Hub B without mapping on Hub B

**Preconditions:** User is hub admin on Hub A only.

**Steps:**
1. Sign in and select Hub A.
2. Attempt API/UI action scoped to Hub B (direct URL or API tampering).

**Expected:** Request rejected. Hub B connections and SSA not visible.

---

### TC-AUTH-06 — Session expires and user must re-authenticate

**Preconditions:** Active session past TTL.

**Steps:** Perform action after session expiry.

**Expected:** Redirect to login. No silent use of stale refresh tokens for product access.

---

### TC-AUTH-07 — OAuth callback handles invalid/expired authorization code gracefully

**Preconditions:** Tampered or expired OAuth callback.

**Steps:** Hit callback with invalid `code`.

**Expected:** User-friendly error. No partial tenant records.

---

### TC-AUTH-08 — Only hub_admin role stored in tenant_users

**Preconditions:** Hub admin completes first login.

**Steps:** Inspect `tenant_users` row for user.

**Expected:** `role = hub_admin`. No project_admin or viewer rows created.

---

## 2. Session & Hub Context

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-SESS-01 | Session stores user_id, tenant_id (hub_id), and connection_id when set | FR-02 | P0 |
| TC-SESS-02 | Multi-hub admin sees hub picker after login | FR-03, BO-03, Scenario C | P0 |
| TC-SESS-03 | Switching hub updates tenant context and loads correct SSA/connections | FR-03, BR-06 | P0 |
| TC-SESS-04 | Actions after hub switch apply only to selected hub | FR-03, BR-01 | P0 |
| TC-SESS-05 | Single-hub admin skips picker or sees only one hub | FR-03 | P1 |
| TC-SESS-06 | Connection context cleared or updated when switching hub | FR-02 | P1 |
| TC-SESS-07 | acc_account_id used for API routing only — not as session tenant key | BR-02 | P0 |

### TC-SESS-01 — Session stores user_id, tenant_id, and connection_id

**Preconditions:** Hub admin logged in, hub and connection selected.

**Steps:** Inspect session after project + Databricks selection.

**Expected:** `session.user_id` = APS user ID; `session.tenant_id` = hub_id; `session.connection_id` set when applicable.

---

### TC-SESS-02 — Multi-hub admin sees hub picker after login

**Preconditions:** User is hub admin on Hub A and Hub B.

**Steps:** Sign in.

**Expected:** Hub picker shown (e.g. "You admin 2 hubs — set up which one?").

---

### TC-SESS-03 — Switching hub updates tenant context

**Preconditions:** User admin on Hub A and Hub B; Hub A selected.

**Steps:** Switch to Hub B in UI.

**Expected:** Hub B SSA status and connections load. Hub A data not shown.

---

### TC-SESS-04 — Actions after hub switch apply only to selected hub

**Preconditions:** Multi-hub admin on Hub B after switch.

**Steps:** Create connection or view dashboard.

**Expected:** All operations scoped to Hub B `hub_id` only.

---

### TC-SESS-05 — Single-hub admin skips picker or sees only one hub

**Preconditions:** User admin on exactly one hub.

**Steps:** Sign in.

**Expected:** Direct to that hub's onboarding/dashboard (no multi-hub confusion).

---

### TC-SESS-06 — Connection context cleared or updated when switching hub

**Preconditions:** Connection selected on Hub A.

**Steps:** Switch to Hub B.

**Expected:** Hub A connection not active; user picks Hub B connection or starts new flow.

---

### TC-SESS-07 — acc_account_id not used as tenant key

**Preconditions:** Two hubs under same ACC account.

**Steps:** Verify tenant isolation uses `hub_id`, not `acc_account_id`.

**Expected:** Separate tenants per hub despite shared account ID.

---

## 3. User & Hub Membership

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-USER-01 | First login creates user-to-hub mapping for hub admin | FR-05, Scenario A | P0 |
| TC-USER-02 | Second hub admin mapped without new SSA creation | FR-06, BR-04, Scenario B | P0 |
| TC-USER-03 | Duplicate mapping not created on repeat login | FR-05, FR-29 | P0 |
| TC-USER-04 | Same user mapped independently to multiple hubs | FR-08, Scenario C | P0 |
| TC-USER-05 | New user + new hub → tenant + SSA + mapping | Decision tree A+B | P0 |
| TC-USER-06 | New user + existing hub → mapping only | Decision tree | P0 |
| TC-USER-07 | Existing user + new hub → new tenant + new SSA | BR-05, Scenario C | P0 |
| TC-USER-08 | UNIQUE(hub_id, aps_user_id) enforced on tenant_users | FR-29 | P1 |
| TC-USER-09 | Hub B admin under same account cannot inherit Hub A mapping | Scenario F | P0 |

### TC-USER-01 — First login creates user-to-hub mapping

**Preconditions:** New hub admin, hub not yet onboarded.

**Steps:** Sign in, select hub.

**Expected:** Row in `tenant_users(hub_id, aps_user_id, role=hub_admin)`.

---

### TC-USER-02 — Second hub admin mapped without new SSA

**Preconditions:** Hub tenant and SSA exist (Alice onboarded).

**Steps:** Bob (hub admin) signs in, selects same hub.

**Expected:** Bob added to `tenant_users`. No second SSA API call.

---

### TC-USER-03 — Duplicate mapping not created on repeat login

**Preconditions:** User already mapped to hub.

**Steps:** Sign in again, select same hub.

**Expected:** Existing mapping reused. No duplicate rows.

---

### TC-USER-04 — Same user mapped to multiple hubs independently

**Preconditions:** User is hub admin on Hub A and Hub B.

**Steps:** Complete login flow for each hub.

**Expected:** Two `tenant_users` rows (one per hub). Independent contexts.

---

### TC-USER-05 — New user + new hub creates full stack

**Preconditions:** Brand-new hub, brand-new admin user.

**Steps:** OAuth → hub pick → onboarding.

**Expected:** `tenants`, `tenant_users`, `ssa_credentials` (after whitelist), first `connection` as applicable.

---

### TC-USER-06 — New user + existing hub adds mapping only

**Preconditions:** Hub fully onboarded by another admin.

**Steps:** New admin signs in and selects hub.

**Expected:** Only `tenant_users` insert. SSA and connections reused.

---

### TC-USER-07 — Existing user + new hub gets new tenant and SSA

**Preconditions:** User already mapped to Hub A; now admin on new Hub B.

**Steps:** Select Hub B, complete onboarding.

**Expected:** New `tenants` row for Hub B, new SSA, separate connections.

---

### TC-USER-08 — UNIQUE constraint on tenant_users

**Preconditions:** Existing (hub_id, aps_user_id) row.

**Steps:** Attempt duplicate insert (concurrent or retry).

**Expected:** Database rejects duplicate. Application handles gracefully.

---

### TC-USER-09 — Hub B admin cannot inherit Hub A setup

**Preconditions:** Same ACC account; Hub A onboarded by Person A.

**Steps:** Person B (Hub B admin only) signs in for Hub B.

**Expected:** Hub B requires independent whitelist + SSA. Hub A credentials not used.

---

## 4. SSA Provisioning & Hub Tenant

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-SSA-01 | At most one SSA robot created per hub | FR-09, BR-03, BO-02 | P0 |
| TC-SSA-02 | Idempotent SSA — reuse existing robot for hub | FR-10, FR-11 | P0 |
| TC-SSA-03 | No SSA create API call when credentials already stored | FR-11, Success criteria | P0 |
| TC-SSA-04 | SSA credentials stored in vault, linked to hub_id only | FR-12, NFR-01 | P0 |
| TC-SSA-05 | Robot exists but not on project — guide add, do not recreate | FR-13 | P0 |
| TC-SSA-06 | Hub onboarding status: pending_whitelist → whitelist_verified → ssa_active | FR-14 | P0 |
| TC-SSA-07 | ensure_ssa(hub_id) returns existing when row present | M2M idempotency | P0 |
| TC-SSA-08 | Second admin completing onboarding does not trigger SSA create | BR-07, Scenario B | P0 |
| TC-SSA-09 | UNIQUE(hub_id) on tenants / ssa_credentials | FR-29 | P0 |
| TC-SSA-10 | SSA private key not in app config or logs | NFR-01 | P0 |
| TC-SSA-11 | Robot email displayed for project invitation — not used in API auth | BR-10 | P1 |
| TC-SSA-12 | Respect 10-robot-per-Client-ID limit — no redundant creates | BR-09 | P0 |

### TC-SSA-01 — One SSA per hub

**Preconditions:** Hub A onboarding complete.

**Steps:** Attempt second SSA provision for Hub A.

**Expected:** System reuses existing robot. Only one row in `ssa_credentials` for hub.

---

### TC-SSA-02 — Idempotent SSA reuse

**Preconditions:** SSA exists for hub from interrupted onboarding.

**Steps:** Admin clicks "Provision" or resumes wizard.

**Expected:** Existing credentials returned. No duplicate API POST.

---

### TC-SSA-03 — Zero SSA API calls when credentials exist

**Preconditions:** `ssa_credentials` populated for hub.

**Steps:** Monitor API during re-onboarding.

**Expected:** No `POST /authentication/v2/service-accounts` for that hub.

---

### TC-SSA-04 — SSA stored in vault per hub

**Preconditions:** SSA provisioned.

**Steps:** Verify storage location and association.

**Expected:** Private key in vault reference only. Tied exclusively to `hub_id`.

---

### TC-SSA-05 — Robot not on project — guide add existing robot

**Preconditions:** SSA created; robot not invited to target ACC project.

**Steps:** Continue connection setup.

**Expected:** UI instructs admin to add existing robot email to project. No new robot created.

---

### TC-SSA-06 — Hub onboarding status transitions

**Preconditions:** Fresh hub.

**Steps:** Progress through whitelist and SSA steps.

**Expected:** Status moves `pending_whitelist` → `whitelist_verified` → `ssa_active` on `tenants`.

---

### TC-SSA-07 — ensure_ssa idempotency function

**Preconditions:** Unit/integration test of `ensure_ssa(hub_id)`.

**Steps:** Call twice for same hub_id.

**Expected:** Second call no-op; same service_account_id returned.

---

### TC-SSA-08 — Second admin skips SSA creation

**Preconditions:** SSA active; Bob first login after Alice.

**Steps:** Bob proceeds through hub onboarding.

**Expected:** Skips SSA step; sees existing status.

---

### TC-SSA-09 — UNIQUE hub_id constraint

**Preconditions:** Tenant exists for hub.

**Steps:** Attempt second tenant row for same hub_id.

**Expected:** Constraint violation prevented.

---

### TC-SSA-10 — Private key not in logs or config

**Preconditions:** SSA provisioning and sync runs.

**Steps:** Review logs and env/config files.

**Expected:** No private key material exposed.

---

### TC-SSA-11 — Robot email for invitation only

**Preconditions:** SSA provisioned.

**Steps:** Verify auth path for scheduled sync.

**Expected:** JWT uses service_account_id + private key; robot email not in API auth.

---

### TC-SSA-12 — No redundant creates near 10-robot limit

**Preconditions:** Customer near Client ID robot limit.

**Steps:** Multiple admins retry provisioning on same hub.

**Expected:** Always reuse; never exceed limit due to duplicates.

---

## 5. Custom Integration & Whitelisting

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-WL-01 | System detects whether Client ID is whitelisted for hub | FR-15 | P0 |
| TC-WL-02 | Progression blocked until whitelist verified | FR-16 | P0 |
| TC-WL-03 | Actionable guidance shown for manual whitelist step | FR-16, FR-17 | P0 |
| TC-WL-04 | Whitelisting not automated — no false "verified" without admin action | FR-17 | P0 |
| TC-WL-05 | After whitelist verified, user can proceed to project/Databricks setup | FR-16 | P0 |
| TC-WL-06 | Whitelist status independent per hub | BR-01, Scenario F | P0 |

### TC-WL-01 — Detect whitelist status

**Preconditions:** Hub with unknown whitelist state.

**Steps:** Admin selects hub after login.

**Expected:** System reports whitelisted or not for vendor Client ID on that hub.

---

### TC-WL-02 — Block until whitelist verified

**Preconditions:** Client ID not whitelisted.

**Steps:** Attempt to skip to project/Databricks setup.

**Expected:** Blocked with instructions. Connection not created.

---

### TC-WL-03 — Actionable whitelist guidance

**Preconditions:** Not whitelisted.

**Steps:** View onboarding screen.

**Expected:** Clear steps: where in ACC Custom Integration to add Client ID.

---

### TC-WL-04 — No automated whitelist

**Preconditions:** Fresh hub.

**Steps:** Complete onboarding without manual ACC action.

**Expected:** System never marks whitelist verified without verification check.

---

### TC-WL-05 — Proceed after whitelist verified

**Preconditions:** Admin whitelisted Client ID in ACC.

**Steps:** Refresh/re-check status in connector.

**Expected:** Unblock project and Databricks selection.

---

### TC-WL-06 — Per-hub whitelist independence

**Preconditions:** Hub A whitelisted; Hub B not.

**Steps:** Admin switches to Hub B.

**Expected:** Hub B still shows pending whitelist. Hub A status unchanged.

---

## 6. Connections

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-CONN-01 | Connection unique by hub + project + workspace + catalog | FR-18, BR-11 | P0 |
| TC-CONN-02 | Existing combination resumes from stored state — no full re-bootstrap | FR-19, BO-06 | P0 |
| TC-CONN-03 | New ACC project creates new connection (same hub) | FR-20, Scenario D | P0 |
| TC-CONN-04 | New Databricks workspace/catalog creates new connection | FR-20, Scenario D | P0 |
| TC-CONN-05 | All hub admins see shared connections for hub | FR-21, Scenario B | P0 |
| TC-CONN-06 | Connection onboarding status enum progression | FR-22 | P0 |
| TC-CONN-07 | Each connection owns pipeline IDs, bootstrap, watermarks, sync history | FR-23 | P0 |
| TC-CONN-08 | One pipeline per catalog — duplicate attempt resumes existing | FR-30, BR-11 | P0 |
| TC-CONN-09 | connection_id = hash(hub_id, project_id, workspace, catalog) | M2M_req | P0 |
| TC-CONN-10 | UNIQUE(connection_id) enforced | FR-29 | P1 |
| TC-CONN-11 | Resume interrupted connection onboarding | Scenario E | P0 |
| TC-CONN-12 | Existing connections unchanged when new connection added | Scenario D | P1 |
| TC-CONN-13 | Bootstrap state per connection — not per user | Migration §14 | P0 |
| TC-CONN-14 | Connection display shows project and Databricks target (business terms) | UI alignment | P2 |

### TC-CONN-01 — Connection uniqueness key

**Preconditions:** Hub with active SSA.

**Steps:** Create connection for (hub, project P1, workspace W1, catalog C1).

**Expected:** Single `connection_id` for that quadruple.

---

### TC-CONN-02 — Resume existing connection

**Preconditions:** Partial or complete connection exists for same target.

**Steps:** Admin selects same project + workspace + catalog again.

**Expected:** Resume dashboard/onboarding. No duplicate bootstrap from scratch.

---

### TC-CONN-03 — New project → new connection

**Preconditions:** Connection exists for Project A.

**Steps:** Create connection for Project B (same hub, same Databricks target).

**Expected:** New `connection_id`. Separate bootstrap.

---

### TC-CONN-04 — New Databricks target → new connection

**Preconditions:** Connection for catalog C1.

**Steps:** Add connection for same project but catalog C2.

**Expected:** New connection. Both active under same hub SSA.

---

### TC-CONN-05 — Shared connections across hub admins

**Preconditions:** Alice created connection; Bob mapped to same hub.

**Steps:** Bob views dashboard.

**Expected:** Bob sees same connections as Alice.

---

### TC-CONN-06 — Connection onboarding status flow

**Preconditions:** New connection.

**Steps:** Progress through setup.

**Expected:** Status follows: `pending_custom_integration` → `pending_ssa_provisioned` → `pending_databricks` → `pending_bootstrap` → `ready`.

---

### TC-CONN-07 — Per-connection operational state

**Preconditions:** Two connections on same hub.

**Steps:** Run sync on one; inspect DB.

**Expected:** Each has own `snapshot_pipeline_id`, `cdc_pipeline_id`, watermarks, `sync_runs`.

---

### TC-CONN-08 — One pipeline per catalog

**Preconditions:** Connection exists for catalog C1.

**Steps:** Attempt second connection to same hub + project + workspace + C1.

**Expected:** System resumes existing connection; no second pipeline.

---

### TC-CONN-09 — connection_id derivation

**Preconditions:** Known inputs.

**Steps:** Create connection; verify ID generation.

**Expected:** Deterministic hash of (hub_id, project_id, dbx_workspace_url, catalog).

---

### TC-CONN-10 — UNIQUE connection_id

**Preconditions:** Existing connection.

**Steps:** Force duplicate insert.

**Expected:** Constraint prevents duplicate.

---

### TC-CONN-11 — Resume interrupted onboarding

**Preconditions:** Connection stuck at `pending_databricks`.

**Steps:** User signs in, selects hub and same target.

**Expected:** Wizard resumes at Databricks step. No duplicate connection.

---

### TC-CONN-12 — Existing connections unchanged on new add

**Preconditions:** Connection A ready.

**Steps:** Add Connection B.

**Expected:** Connection A pipelines and sync schedule unaffected.

---

### TC-CONN-13 — Bootstrap state on connection not user

**Preconditions:** Two admins, one connection.

**Steps:** Alice starts bootstrap; Bob logs in mid-flight.

**Expected:** Bob sees same connection bootstrap state (not user-scoped POC state).

---

### TC-CONN-14 — Connection display uses business terms

**Preconditions:** Connection exists.

**Steps:** View connection list.

**Expected:** Hub, Project, Workspace, Catalog shown; technical IDs secondary.

---

## 7. Sync Operations

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-SYNC-01 | Interactive onboarding uses user OAuth (U2M) where appropriate | FR-24 | P0 |
| TC-SYNC-02 | Scheduled sync uses hub SSA (M2M JWT-bearer) for ACC | FR-25, BR-08, BO-05 | P0 |
| TC-SYNC-03 | Scheduled sync uses Databricks service principal (M2M) | FR-26 | P0 |
| TC-SYNC-04 | Scheduled sync never uses human refresh tokens | FR-04, BR-08 | P0 |
| TC-SYNC-05 | Sync failure attributed to connection and run phase | FR-27 | P0 |
| TC-SYNC-06 | Retry after sync failure is idempotent | FR-27 | P0 |
| TC-SYNC-07 | Headless sync succeeds without user session | Success criteria | P0 |
| TC-SYNC-08 | CDC incremental sync after baseline bootstrap | Pipeline overview | P1 |
| TC-SYNC-09 | Data Connector API uses acc_account_id in URL path only | BR-02, M2M_req | P1 |
| TC-SYNC-10 | Sync run history recorded per connection | FR-23 | P1 |

### TC-SYNC-01 — U2M for interactive flows

**Preconditions:** User in onboarding wizard (e.g. Reviews).

**Steps:** Complete step requiring ACC user context.

**Expected:** 3LO user token used; not SSA for interactive-only steps.

---

### TC-SYNC-02 — M2M ACC auth on schedule

**Preconditions:** Connection `ready`; scheduler triggers.

**Steps:** Run scheduled sync; inspect ACC auth.

**Expected:** JWT bearer from hub's SSA credentials.

---

### TC-SYNC-03 — M2M Databricks auth on schedule

**Preconditions:** Connection with SP configured.

**Steps:** Run scheduled sync.

**Expected:** Databricks `client_credentials` for service principal.

---

### TC-SYNC-04 — No human refresh token in scheduler

**Preconditions:** No user logged in.

**Steps:** Execute scheduled job.

**Expected:** Job succeeds/fails without any user refresh token in auth path.

---

### TC-SYNC-05 — Failure attribution

**Preconditions:** Simulated ACC or Databricks failure mid-sync.

**Steps:** Inspect sync run record.

**Expected:** Error linked to `connection_id`, run ID, and phase (export/ingest/CDC).

---

### TC-SYNC-06 — Idempotent retry

**Preconditions:** Failed sync mid-batch.

**Steps:** Retry sync.

**Expected:** No duplicate Bronze rows or broken watermarks; safe resume.

---

### TC-SYNC-07 — Headless reliability

**Preconditions:** Production-like schedule over 24h.

**Steps:** Monitor scheduled runs.

**Expected:** Runs complete using hub SSA only.

---

### TC-SYNC-08 — CDC after bootstrap

**Preconditions:** Bootstrap complete.

**Steps:** Trigger incremental sync.

**Expected:** CDC pipeline runs; watermarks advance.

---

### TC-SYNC-09 — acc_account_id in DC API paths only

**Preconditions:** Sync export request.

**Steps:** Inspect API URL.

**Expected:** `/accounts/{acc_account_id}/requests` used; tenant logic still hub-scoped.

---

### TC-SYNC-10 — Sync run history

**Preconditions:** Multiple sync runs.

**Steps:** View connection history.

**Expected:** `sync_runs` rows with timestamps, status, phase.

---

## 8. Concurrency & Data Integrity

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-CONC-01 | Simultaneous SSA provision for same hub → exactly one robot | FR-28 | P0 |
| TC-CONC-02 | Transactional create with UNIQUE(hub_id) under race | FR-28, NFR-03 | P0 |
| TC-CONC-03 | Simultaneous connection create for same ID → one connection | FR-29 | P1 |
| TC-CONC-04 | One tenant per hub enforced | FR-29 | P0 |
| TC-CONC-05 | One user mapping per hub-user pair enforced | FR-29 | P0 |
| TC-CONC-06 | One SSA credential set per hub enforced | FR-29 | P0 |

### TC-CONC-01 — Concurrent SSA provision

**Preconditions:** Two admins click Provision simultaneously on new hub.

**Steps:** Parallel requests to create SSA.

**Expected:** Exactly one robot in APS and one row in `ssa_credentials`.

---

### TC-CONC-02 — Transactional uniqueness on race

**Preconditions:** DB under load.

**Steps:** Duplicate concurrent tenant/SSA inserts.

**Expected:** One succeeds; other gets conflict and reads existing.

---

### TC-CONC-03 — Concurrent duplicate connection create

**Preconditions:** Same connection target selected twice in parallel.

**Steps:** Two create requests.

**Expected:** One connection; second resumes existing.

---

### TC-CONC-04 — One tenant per hub

**Preconditions:** Race on tenant insert.

**Expected:** UNIQUE(hub_id) on tenants holds.

---

### TC-CONC-05 — One mapping per hub-user pair

**Preconditions:** Double login callback race.

**Expected:** Single tenant_users row per (hub_id, aps_user_id).

---

### TC-CONC-06 — One SSA set per hub

**Preconditions:** Retry storm on SSA create.

**Expected:** ssa_credentials UNIQUE on tenant_id (hub_id).

---

## 9. End-to-End User Scenarios

| ID | Title | Scenario | Priority |
|----|-------|----------|----------|
| TC-E2E-A | First hub admin, new hub — full onboarding | 9.1 | P0 |
| TC-E2E-B | Second admin joins existing hub | 9.2 | P0 |
| TC-E2E-C | One admin, multiple hubs | 9.3 | P0 |
| TC-E2E-D | Same hub, new project or Databricks target | 9.4 | P0 |
| TC-E2E-E | Resume interrupted onboarding | 9.5 | P0 |
| TC-E2E-F | Different hub admins under same ACC account | 9.6 | P0 |

### TC-E2E-A — First hub admin, new hub

**Flow:** OAuth → hub select → tenant + mapping → whitelist guide → SSA once → project + Databricks → connection + bootstrap → sync enabled.

**Outcome:** One SSA, one or more connections, user mapped as hub admin.

---

### TC-E2E-B — Second admin joins existing hub

**Flow:** Bob OAuth → same hub → mapping only → sees existing connections/wizard state.

**Outcome:** Two mappings, one SSA, shared connections.

---

### TC-E2E-C — One admin, multiple hubs

**Flow:** Login → hub picker → Hub A context → switch Hub B → independent SSA and connections.

**Outcome:** No cross-hub data leakage.

---

### TC-E2E-D — New project or Databricks target

**Flow:** New connection wizard → different project and/or catalog → new connection ID → bootstrap.

**Outcome:** Hub SSA reused; multiple independent connections.

---

### TC-E2E-E — Resume interrupted onboarding

**Flow:** Login → read onboarding_status → resume at last incomplete step.

**Outcome:** No duplicate robots or connections.

---

### TC-E2E-F — Different hub admins, same account

**Flow:** Hub A admin completes setup; Hub B admin must whitelist + SSA independently.

**Outcome:** Hub-scoped isolation validated.

---

## 10. Security & Non-Functional

| ID | Title | Req | Priority |
|----|-------|-----|----------|
| TC-NFR-01 | SSA private keys in vault only | NFR-01 | P0 |
| TC-NFR-02 | Tenant isolation — no cross-hub access | NFR-02 | P0 |
| TC-NFR-03 | Idempotent provisioning under concurrent admins | NFR-03 | P0 |
| TC-NFR-04 | Align with Autodesk SSA GA and tenant isolation guidance | NFR-04 | P1 |
| TC-NFR-05 | Onboarding and sync states visible in UI (Pipeline Strip) | NFR-05 | P1 |
| TC-NFR-06 | Model supports multi-hub, multi-connection, multi-user | NFR-06 | P1 |
| TC-NFR-07 | Support metrics: zero duplicate robots; reduced wrong-hub tickets | Success criteria | P1 |

### TC-NFR-01 — Vault storage for SSA keys

**Steps:** Audit deployment config and runtime.

**Expected:** Keys in vault reference; not in repo or plain env.

---

### TC-NFR-02 — Tenant isolation audit

**Steps:** Attempt cross-tenant data access via API/session manipulation.

**Expected:** All attempts denied without hub_admin mapping on target hub.

---

### TC-NFR-03 — Concurrent idempotency

**Steps:** Load test parallel onboarding on same hub.

**Expected:** Consistent final state; no duplicates.

---

### TC-NFR-04 — SSA policy compliance

**Steps:** Review integration against APS SSA GA docs.

**Expected:** JWT flow, key rotation path, tenant isolation documented and met.

---

### TC-NFR-05 — Visible pipeline/onboarding state

**Steps:** View dashboard during partial onboarding.

**Expected:** Clear status per hub and connection (aligned with UI_DESIGN).

---

### TC-NFR-06 — Scalability smoke test

**Steps:** Org with multiple hubs, 5+ connections, 3+ admins per hub.

**Expected:** UI and API perform acceptably; data model holds.

---

### TC-NFR-07 — Operational success metrics

**Steps:** Track SSA API calls and support tickets post-release.

**Expected:** No duplicate robot creates; faster second-admin onboarding.

---

## 11. POC → Production Migration

| ID | Title | Migration row | Priority |
|----|-------|---------------|----------|
| TC-MIG-01 | Session tracks user + hub + connection (not user only) | §14 | P0 |
| TC-MIG-02 | Connections keyed by hub + project + Databricks (not per-user acc_config) | §14 | P0 |
| TC-MIG-03 | Per-hub SSA in vault (not global .env SSA) | §14 | P0 |
| TC-MIG-04 | m2m tables aligned with tenants + connections model | §14, M2M mapping | P1 |

### TC-MIG-01 — Session model upgrade

**Expected:** Production session includes `tenant_id` and optional `connection_id`.

---

### TC-MIG-02 — Connection model replaces per-user config

**Expected:** No user-scoped acc_config for pipeline identity.

---

### TC-MIG-03 — Per-hub SSA replaces global env

**Expected:** Each hub reads SSA from `ssa_credentials`/vault.

---

### TC-MIG-04 — M2M tables tenant-aligned

**Expected:** Headless sync tables reference hub tenant and connection IDs.

---

## 12. Requirements Traceability Matrix

| Requirement IDs | Test case IDs |
|-----------------|---------------|
| FR-01, FR-01a | TC-AUTH-01–08 |
| FR-02, FR-03 | TC-SESS-01–07 |
| FR-04, FR-25, BR-08 | TC-SYNC-02–04, TC-SYNC-07 |
| FR-05–FR-08 | TC-USER-01–09 |
| FR-09–FR-14 | TC-SSA-01–12 |
| FR-15–FR-17 | TC-WL-01–06 |
| FR-18–FR-23, FR-30 | TC-CONN-01–14 |
| FR-24–FR-27 | TC-SYNC-01–10 |
| FR-28, FR-29 | TC-CONC-01–06, TC-SSA-09, TC-USER-08 |
| BO-01–BO-08 | TC-SESS-03–07, TC-AUTH-02–05, TC-SSA-01, TC-SYNC-07 |
| BR-01–BR-12 | Covered across TC-SESS, TC-SSA, TC-CONN, TC-AUTH, TC-WL |
| NFR-01–NFR-06 | TC-NFR-01–07 |
| Scenarios A–F | TC-E2E-A–F |
| POC Migration §14 | TC-MIG-01–04 |

---

## 13. Out of Scope (not tested in v1)

These items are explicitly **out of scope** per BUSINESS_REQUIREMENTS.md §4.2 — no test cases required unless product scope changes:

- Automating Custom Integration whitelisting
- Cross-hub SSA sharing (default model)
- Project admin / viewer product access
- Org-level billing or quota management
- Email/webhook notifications for failed runs (future)

---

*Generated from BUSINESS_REQUIREMENTS.md and M2M_req 1.md. For BrowserStack Test Management import, upload this file to your BrowserStack project and run the Test Case Generator agent.*
