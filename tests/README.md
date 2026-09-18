# Test suite — how to run it, and what covers which requirement

```bash
cd acc-connector
.\.venv\Scripts\Activate.ps1        # PowerShell; source .venv/bin/activate on Unix
python -m unittest discover -s tests
node tests/sync_button_state.test.js
node tests/portal_state.test.js
python scripts/smoke_check.py
```

`SECRET_KEY` must be set (local dev reads `acc-connector/.env`; CI passes a dummy). Repository
tests inherit `_dbharness.TempDbTestCase`, which points `database.DB_PATH` at a per-test temp
file and selects the in-memory SecretStore, so no test can touch a developer's `connector.db`
or write a real secret.

The U2M path must also stay green with the shared-step kill switch flipped:

```bash
USE_SHARED_STEPS=true python -m unittest discover -s tests
```

## FR-07 coverage map

Every one of FR-07's 83 test-case identifiers is listed. `automated` names the file that
covers it; `manual` means it needs a real ACC hub or a real Databricks workspace and belongs
to the release checklist, not this suite.

### Auth and access control (FR-02 FR-01a, BR-12)

| TC | What it asserts | Where |
|---|---|---|
| TC-AUTH-01 | Hub admin signs in via Autodesk OAuth | manual — real APS OAuth round trip |
| TC-AUTH-02 | Project admin denied after OAuth | `test_portal_routes.py`, `test_hub_admin.py` |
| TC-AUTH-03 | Viewer-only user denied | `test_hub_admin.py` |
| TC-AUTH-04 | No hub-admin role anywhere → clear denial | `test_portal_routes.py` |
| TC-AUTH-05 | Hub A admin cannot reach Hub B | `test_tenant_auth.py`, `test_connection_routes.py`, `test_portal_routes.py` |
| TC-AUTH-06 | Expired session forces re-auth | partial — `test_tenant_auth.py` covers "no session → 401"; real cookie expiry is manual |
| TC-AUTH-07 | Invalid/expired auth code handled | manual — APS error round trip |
| TC-AUTH-08 | Only `hub_admin` stored in `tenant_users` | `test_tenant_repos.py` |

### Session and hub switching (FR-02 FR-02/03)

| TC | What it asserts | Where |
|---|---|---|
| TC-SESS-01 | Session holds user + hub + connection | `test_tenant_auth.py` |
| TC-SESS-02 | Multi-hub admin sees the picker | `test_portal_routes.py` for the hub list `/api/me` returns; the picker itself renders client-side — manual |
| TC-SESS-03 | Switching hub loads that hub only | `test_portal_routes.py` (`test_selecting_a_hub_sets_the_session`), `test_connection_routes.py` |
| TC-SESS-04 | Actions after a switch apply to the new hub | `test_tenant_auth.py`, `test_portal_routes.py` (`test_cannot_read_another_hub_by_url`) |
| TC-SESS-05 | Single-hub admin skips the picker | manual — the skip is a client-side branch in `app.js` `init()` |
| TC-SESS-06 | Hub switch clears connection context | `test_tenant_auth.py` |
| TC-SESS-07 | `acc_account_id` is routing only, never the tenant key | `test_connection_runner.py` |

### User ↔ hub mapping (FR-02 FR-05…08)

| TC | What it asserts | Where |
|---|---|---|
| TC-USER-01 | First login creates the mapping | `test_portal_routes.py` |
| TC-USER-02 | Second hub admin mapped, no second robot | `test_ssa_provisioner.py` |
| TC-USER-03 | Repeat login does not duplicate the mapping | `test_tenant_repos.py` |
| TC-USER-04 | One user mapped to several hubs independently | `test_tenant_repos.py` |
| TC-USER-05 | New user + new hub → tenant + SSA + mapping | `test_ssa_provisioner.py` (tenant + robot), `test_portal_routes.py` (mapping) |
| TC-USER-06 | New user + existing hub → mapping only | `test_ssa_provisioner.py` (`test_second_admin_reuses_the_robot`), `test_portal_routes.py` |
| TC-USER-07 | Existing user + new hub → new tenant + new SSA | `test_ssa_provisioner.py` (`test_different_hubs_get_different_robots`) |
| TC-USER-08 | `PK(tenant_users)` enforced | `test_schema_init.py`, `test_tenant_repos.py` |
| TC-USER-09 | Two hubs under one account are separate tenants | `test_tenant_repos.py` |

### SSA provisioning (FR-02 FR-09…14, FR-28)

| TC | What it asserts | Where |
|---|---|---|
| TC-SSA-01 | Robot created for a new hub | `test_ssa_provisioner.py` |
| TC-SSA-02 | Exactly one robot per hub | `test_ssa_provisioner.py` |
| TC-SSA-03 | Created through the APS service-accounts API | `test_ssa_client.py` (`test_posts_to_service_accounts_with_bearer`) |
| TC-SSA-04 | Key in the vault, only a ref in the DB | `test_ssa_provisioner.py`, `test_secret_store.py` |
| TC-SSA-05 | Robot exists but not on the project → guide, never recreate | `test_provisioning_probe.py` |
| TC-SSA-06 | Status progression pending → verified → active | `test_ssa_provisioner.py` (pending_whitelist), `test_provisioning_probe.py` (verified), `test_connection_service.py` (ssa_active on first ready connection) |
| TC-SSA-07 | `ensure_ssa` returns the existing row | `test_ssa_provisioner.py` (`test_second_call_makes_zero_create_calls`) |
| TC-SSA-08 | Second admin finishing onboarding creates nothing | `test_ssa_provisioner.py` |
| TC-SSA-09 | `UNIQUE(hub_id)` on tenants and ssa_credentials | `test_schema_init.py` |
| TC-SSA-10 | No private key in config or logs | `test_ssa_provisioner.py`, `test_onboarding_routes.py` |
| TC-SSA-11 | Robot email is for the invitation, not API auth | `test_ssa_token_service.py`, `test_onboarding_routes.py` |
| TC-SSA-12 | 10-robot ceiling respected, no redundant creates | `test_aps_capacity.py` |

### Whitelist gating (FR-02 FR-15…17)

| TC | What it asserts | Where |
|---|---|---|
| TC-WL-01 | Detects whether the Client ID is whitelisted | `test_provisioning_probe.py` |
| TC-WL-02 | Progression blocked until verified | `test_provisioning_probe.py`, `test_onboarding_routes.py` |
| TC-WL-03 | Actionable guidance for the manual step | `test_provisioning_probe.py` |
| TC-WL-04 | No false "verified" without the admin acting | `test_provisioning_probe.py` |
| TC-WL-05 | After verification the admin may proceed | `test_provisioning_probe.py` |
| TC-WL-06 | Whitelist status is independent per hub | `test_provisioning_probe.py` |

### Connections (FR-02 FR-18…23, FR-30, BR-11)

| TC | What it asserts | Where |
|---|---|---|
| TC-CONN-01 | Unique by hub + project + workspace + catalog | `test_connection_service.py`, `test_connection_identity.py` |
| TC-CONN-02 | Existing combination resumes, no re-bootstrap | `test_connection_service.py` |
| TC-CONN-03 | New ACC project → new connection | `test_connection_service.py` |
| TC-CONN-04 | New workspace/catalog → new connection | `test_connection_service.py` |
| TC-CONN-05 | Connections shared by all of a hub's admins | `test_connection_repos.py`, `test_connection_routes.py` |
| TC-CONN-06 | Status enum progression | `test_connection_service.py`, `test_connection_repos.py` |
| TC-CONN-07 | Each connection owns its pipelines, bootstrap, watermarks, history | `test_connection_repos.py` |
| TC-CONN-08 | One pipeline per catalog; a duplicate resumes | `test_connection_service.py` |
| TC-CONN-09 | `connection_id` is the FR-05 §7 hash | `test_connection_identity.py` |
| TC-CONN-10 | `UNIQUE(connection_id)` enforced | `test_schema_init.py` |
| TC-CONN-11 | Wizard resumes at the right step | `test_connection_service.py`, `portal_state.test.js` |
| TC-CONN-12 | Per-connection run history | `test_connection_repos.py`, `test_connection_routes.py` |
| TC-CONN-13 | Watermarks are per connection | `test_connection_repos.py`, `test_connection_runner.py` |
| TC-CONN-14 | Deleting a connection removes its child rows | `test_connection_repos.py::DeleteTests` |

### Sync (FR-02 FR-24…27, BR-08)

| TC | What it asserts | Where |
|---|---|---|
| TC-SYNC-01 | Manual sync may use the signed-in user's tokens | `test_connection_runner.py` |
| TC-SYNC-02 | Scheduled sync uses the hub SSA | `test_connection_runner.py` |
| TC-SYNC-03 | Scheduled sync uses the Databricks service principal | `test_connection_runner.py`, `test_dbx_m2m_token.py` |
| TC-SYNC-04 | Scheduled sync never reads a human refresh token | `test_connection_runner.py`, `test_ssa_token_service.py` |
| TC-SYNC-05 | Failure attributed to connection, run and phase | `test_connection_runner.py` |
| TC-SYNC-06 | Retry after failure is idempotent, window unchanged | `test_connection_runner.py` |
| TC-SYNC-07 | Headless sync with no user session | `test_scheduler.py`, `test_connection_routes.py` |
| TC-SYNC-08 | CDC incremental after a snapshot baseline | `test_sync_steps.py`, `test_connection_runner.py` |
| TC-SYNC-09 | `acc_account_id` in the DC URL path only | `test_connection_runner.py` |
| TC-SYNC-10 | Run history recorded per connection | `test_connection_routes.py`, `test_connection_repos.py` |

### Concurrency (FR-02 FR-28, BR-09)

| TC | What it asserts | Where |
|---|---|---|
| TC-CONC-01 | Simultaneous provision → exactly one robot | `test_ssa_provisioner.py` |
| TC-CONC-02 | `UNIQUE(hub_id)` holds under a race | `test_ssa_provisioner.py` |
| TC-CONC-03 | Simultaneous connection create → one connection | `test_connection_service.py` |
| TC-CONC-04 | One tenant per hub | `test_schema_init.py` |
| TC-CONC-05 | One mapping per hub-user pair | `test_schema_init.py` |
| TC-CONC-06 | One credential set per hub | `test_ssa_provisioner.py`, `test_schema_init.py` |

### Security and NFRs

| TC | What it asserts | Where |
|---|---|---|
| TC-NFR-01 | Private keys in the vault only | `test_secret_store.py`, `test_onboarding_routes.py`, `test_connection_routes.py` |
| TC-NFR-02 | Tenant isolation, no cross-hub access | `test_tenant_auth.py`, `test_connection_routes.py` |
| TC-NFR-03 | Idempotent provisioning under concurrent admins | `test_ssa_provisioner.py` |
| TC-NFR-04 | Aligns with Autodesk SSA tenant-isolation guidance | manual — design review, not executable |
| TC-NFR-05 | Onboarding and sync states visible in the UI | manual — DOM walkthrough against FR-06 §9 |
| TC-NFR-06 | Multi-hub, multi-connection, multi-user | `test_connection_repos.py`, `test_tenant_repos.py`, `test_connection_routes.py` (a hub sees only its own) |
| TC-NFR-07 | Zero duplicate robots, fewer wrong-hub tickets | manual — production metric, not a test |

### POC migration

| TC | What it asserts | Where |
|---|---|---|
| TC-MIG-01 | Session tracks user + hub + connection | `test_tenant_auth.py` |
| TC-MIG-02 | Connections keyed by hub + project + Databricks | `test_connection_service.py` |
| TC-MIG-03 | Per-hub SSA in the vault, not a global `.env` robot | `test_ssa_provisioner.py` |
| TC-MIG-04 | Legacy `m2m_*` surface gone | `scripts/smoke_check.py` asserts the modules stay deleted and `app.py` keeps no `ENABLE_M2M`. The legacy **tables** are deliberately still in `database.py` — dropping them is out of scope (FR-05 §11.2). |

## FR-04 coverage map

FR-04's identifiers are its own `TC-01…TC-10` and are unrelated to the `TC-<AREA>-NN` set above.

| TC | What it asserts | Where |
|---|---|---|
| TC-01 | First hub uses App A | `test_aps_capacity.py` |
| TC-02 | Hubs 2–10 use the same app | `test_aps_capacity.py` |
| TC-03 | Hub 11 triggers shard B | **out of scope** — ADR D-13 ships one Client ID at the APS ceiling of 10; the ceiling is enforced and alerted, multi-shard *selection* is deferred |
| TC-04 | Hub 1 admin revisit still mints on App A | `test_aps_capacity.py` |
| TC-05 | Hub 11 wrong whitelist | **out of scope** — same reason as TC-03; with one documented Client ID the question cannot arise |
| TC-06 | `ensure_ssa` idempotent | `test_ssa_provisioner.py` |
| TC-07 | All apps full → friendly `SsaCapacityExhausted` | `test_aps_capacity.py`, `test_onboarding_routes.py` |
| TC-08 | `UNIQUE(service_account_id)` | `test_schema_init.py` |
| TC-09 | Offboarding frees a slot | `test_aps_capacity.py` |
| TC-10 | Concurrent hub-11 provision | **out of scope** — same reason as TC-03 |

## Release checklist — what only a real environment can prove

These are the manual gates from ADR D-5. Phase 1 exits on code-complete plus fake-verified;
these run before a customer is onboarded.

1. Whitelist the connector Client ID in a real ACC hub, then run wizard step 1 and confirm
   probe 1 flips from refused to verified.
2. Provision a robot on that hub, invite it to one project, and confirm probes 2 and 3 pass —
   then connect a *second* project and confirm they are asked again rather than inherited.
3. Full snapshot sync on a real Databricks workspace, both `ENABLE_NOTEBOOK_DOWNLOAD` modes.
4. One CDC cycle, triggered by `POST /internal/scheduler/cdc` with no browser session open.
5. Rotate the robot key mid-life and confirm the next sync mints on the new key.
6. A full U2M wizard walkthrough with `USE_SHARED_STEPS=true` — the soak criterion that
   releases the kill switch for deletion (T31).
