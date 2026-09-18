# Databricks Partner Integration Checklist

**Source:** [Databricks Partner Well Architected Framework — Connected ISV Partners](https://databrickslabs.github.io/partner-architecture/isv-partners/integration-requirements)

**Assessed:** April 28, 2026
**Connector:** ACC Connector v2 (Autodesk Construction Cloud → Azure Databricks)
**Target:** Databricks Validated Technology Partner status

> Use this document to track every requirement Databricks enforces for a validated partner integration, what our current code does, and what we still need to build before requesting Databricks partner validation.

---

## Summary Verdict

| Pillar | Status | Validation-blocking? |
|--------|--------|----------------------|
| **1. Telemetry (User-Agent)** | FAIL | YES |
| **2a. OAuth U2M (PKCE, state, Databricks OIDC)** | PARTIAL | YES |
| **2b. OAuth M2M (WIF / Account Token Federation / OIDC M2M for auto-sync)** | FAIL | NO (recommended) |
| **3. Unity Catalog (namespace, Volumes, managed tables, lineage)** | MOSTLY PASS | NO (lineage is "should") |
| **Cross-cutting (token redaction, disconnect UX)** | PARTIAL | NO |

**Bottom line:** We cannot pass Databricks partner validation today. Two mandatory items are completely missing (User-Agent and PKCE), one is partial (state parameter on OAuth U2M), our hourly auto-sync currently relies on user refresh tokens instead of a proper M2M flow (recommended but not blocking), and a couple of "should" items (lineage, disconnect UX) are open. Total estimated effort to reach validation-ready: **~2 days**. Adding the M2M path for auto-sync is a recommended Phase 2 item (~1 additional day).

---

## Pillar 1 — Telemetry (User-Agent)

### What Databricks requires

Source: [Telemetry & Attribution](https://databrickslabs.github.io/partner-architecture/isv-partners/telemetry-attribution)

Every Databricks API call, SQL connection, SDK invocation, or REST call originating from our product **must** carry a stable `User-Agent` HTTP header in the format:

```
<isv-name>_<product-name>/<product-version>
```

Example: `CCTech_ACCConnector/2.0`

Mandatory rules:

- Underscore (`_`) separator between ISV name and product name (strict)
- Each product/integration must have a distinct User-Agent
- Must be set **programmatically** in the connection code — cannot be configured by the customer
- Applies to: REST APIs, SQL Statement Execution API, Jobs API, Files API (UC Volumes uploads), SDKs, JDBC/ODBC drivers, Iceberg REST clients

### How Databricks validates it

A workspace admin runs:

```sql
SELECT *
FROM system.access.audit
WHERE event_time > current_timestamp() - INTERVAL 2 days
  AND lower(user_agent) LIKE '%cctech_accconnector%';
```

Or checks the **Source** column in Query History UI.

### Our current state

**FAIL.** No `User-Agent` header is set anywhere in the codebase.

[acc-connector/backend/databricks_client.py](acc-connector/backend/databricks_client.py) lines 28-33:

```python
def __init__(self, workspace_url: str, pat: str):
    self.base = workspace_url.rstrip('/')
    self._headers = {
        'Authorization': f'Bearer {pat}',
        'Content-Type':  'application/json',
    }
```

Every call goes out with the default `python-requests/X.Y.Z` User-Agent.

### Fix

Add one header line in `databricks_client.py` and propagate to `put_file()`:

```python
PARTNER_USER_AGENT = 'CCTech_ACCConnector/2.0'

self._headers = {
    'Authorization': f'Bearer {pat}',
    'Content-Type':  'application/json',
    'User-Agent':    PARTNER_USER_AGENT,
}
```

Also add to:
- `put_file()` — already builds a separate headers dict (line 56-60)
- `execute_sql()` — uses `self._headers`, so covered automatically
- The Databricks OAuth token-exchange call in [acc-connector/app.py](acc-connector/app.py) line 1411-1423
- The Databricks workspace validation call in [acc-connector/app.py](acc-connector/app.py) line 1435-1439

**Effort:** 1 hour
**Validation-blocking:** YES

---

## Pillar 2 — OAuth

### What Databricks requires

Source: [Access & Authentication](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth) and [OAuth U2M Implementation](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth/oauth-u2m)

OAuth 2.0 is **mandatory** for all ISV partner integrations. PAT alone is not sufficient for validation.

Authentication method comparison (from the framework):

| Method | Status | Use Case |
|--------|--------|----------|
| Workload Identity Federation (WIF) | **Recommended** | Service principals (M2M) |
| Account-wide Token Federation (U2M) | **Recommended** | User interactive, SSO |
| Account-wide Token Federation (M2M) | Supported | M2M (less isolation) |
| Databricks OIDC (U2M) | Supported | When federation not feasible |
| Databricks OIDC (M2M) | Supported | When federation not feasible |
| Personal Access Tokens | **Limited** | Additional option only — not sufficient alone |

For our connector (user-facing SaaS app), the recommended pattern is **Account-wide Token Federation for U2M**, with Databricks OIDC U2M as the supported fallback. Both require:

- **PKCE** (`code_challenge` + `code_verifier`, S256 method) — always
- **`state` parameter** — random, CSRF protection
- **Refresh token rotation**
- **Token redaction** — never log tokens
- **Disconnect/re-auth experience** in the UI

### Our current state

**PARTIAL.**

We use Azure Entra ID OAuth (`https://login.microsoftonline.com/.../oauth2/v2.0/token`) with the AzureDatabricks resource scope `2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/user_impersonation`. See [acc-connector/app.py](acc-connector/app.py) lines 1366-1460.

This works **only on Azure Databricks** — it will not work on AWS or GCP Databricks deployments. The framework's recommended pattern (Databricks OIDC at `https://{databricks-host}/oidc/v1/authorize` and `/oidc/v1/token`) works across all three clouds.

| Sub-requirement | State | Notes |
|-----------------|-------|-------|
| OAuth flow exists (not PAT-only) | PASS | Upgrade A is done |
| Cross-cloud (AWS/Azure/GCP) | FAIL | Locked to Azure Entra ID |
| `state` parameter | FAIL | No `state` in `/connect/databricks` redirect |
| PKCE (`code_challenge` / `code_verifier`) | FAIL | Not implemented |
| Refresh token persisted | PASS | Stored encrypted in `connector.db` via Fernet |
| Refresh token rotation | PARTIAL | We refresh but don't rotate single-use refresh tokens |
| Token redaction in logs | VERIFY | Audit needed |
| Disconnect/re-auth UI | FAIL | No disconnect button |
| ACC OAuth `state` parameter | FAIL | Already documented in PRODUCTION_READINESS_CHECKLIST.md item #2 |

### Fix — Option A (minimum effort, stays Azure-only)

Keep Entra ID OAuth, add the missing items:

1. Add `state = secrets.token_urlsafe(32)` to the redirect, store in Flask session, validate on callback
2. Add PKCE — generate `code_verifier` (43-128 chars), compute `code_challenge = base64url(sha256(code_verifier))`, send `code_challenge_method=S256`
3. Document Entra ID as Azure-specific path in README; add a note that AWS/GCP deployments will need Databricks OIDC

**Effort:** ~4 hours

### Fix — Option B (recommended, multi-cloud)

Migrate to Databricks OIDC U2M:

1. Customer registers an OAuth app at Databricks Account Console: **Settings → App connections → Add connection**
2. App redirects to `https://{workspace-host}/oidc/v1/authorize?response_type=code&client_id=...&redirect_uri=...&scope=sql&state=...&code_challenge=...&code_challenge_method=S256`
3. Token exchange at `https://{workspace-host}/oidc/v1/token`
4. Use the access token directly as the bearer in our `databricks_client.py` — no Azure-specific scope, works on AWS/GCP/Azure
5. Add disconnect button in UI that revokes refresh token and clears DB rows

New env vars to add:
- `DATABRICKS_OIDC_CLIENT_ID`
- `DATABRICKS_OIDC_CLIENT_SECRET`

Files to change:
- [acc-connector/app.py](acc-connector/app.py) — replace `/connect/databricks` and `/databricks/callback` with Databricks OIDC flow
- [acc-connector/backend/state_store.py](acc-connector/backend/state_store.py) — already supports tokens, no schema change

**Effort:** ~1 day
**Validation-blocking:** YES (PKCE and state are blockers regardless of which option)

### Fix — Token redaction audit

Grep all `logger.info` / `logger.error` / `print()` calls for any reference to `access_token`, `refresh_token`, `pat`, `client_secret`. Add a `_redact_token(s)` helper that replaces tokens with `***REDACTED***` in any log line that includes them.

**Effort:** 2 hours

### Fix — Disconnect/re-auth UX

Add a "Disconnect Databricks" button on Step 2 panel that:

1. Calls `POST /disconnect/databricks`
2. Server-side: clears `dbx_tokens` and `bootstrap_state` rows for the user
3. Calls Databricks token revocation endpoint with the refresh token
4. UI returns to "Sign in with Databricks" CTA

Same pattern for ACC ("Disconnect Autodesk").

**Effort:** half day

---

### M2M (Machine-to-Machine) — implications for hourly auto-sync

Source: [Access & Authentication — Client credentials flow](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth) and [Integration requirements — Client credentials flow](https://databrickslabs.github.io/partner-architecture/isv-partners/integration-requirements)

Databricks defines M2M as automated, non-interactive authentication for "jobs, services, and scripts" with three options ranked by preference:

| Option | Description | Recommendation |
|--------|-------------|----------------|
| **Workload Identity Federation (WIF)** | Per-SP trust policies; eliminates secrets | **Recommended** — tightest security, 1:1 SP mapping |
| **Account Token Federation (M2M)** | Account-wide federation for unified setup | Supported — broader trust, less isolation than WIF |
| **Databricks OIDC M2M** | Uses long-lived client secrets (Service Principal client ID + secret) | Last resort — only when customer doesn't support federated identity |

#### Why M2M matters for our connector

Our hourly auto-sync feature (added in Upgrade F) creates a **gray-zone scenario**:

- When the user is signed in and clicks "Sync Now" → clearly **U2M** (user-driven)
- When our background `threading.Timer` fires every hour with the user offline → **technically automation**, but we currently use the user's stored OAuth refresh token to obtain a new access token

This works today because we persist the user's refresh token (Fernet-encrypted in `connector.db`). But it has problems the M2M pattern would solve:

| Issue with U2M-stored-token approach | M2M solution |
|--------------------------------------|--------------|
| User's refresh token expires (e.g., 90 days inactivity) → auto-sync silently breaks until user re-logs in | Service Principal token refreshes indefinitely without user interaction |
| Audit logs show the sync ran "as the user" even though the user did nothing | Audit logs correctly attribute scheduled syncs to the partner SP |
| If the user is offboarded from the customer's Databricks account, all their auto-syncs die | SP is independent of any one user |
| Compliance teams flag long-lived user tokens as a security concern | Federated SP tokens are short-lived and rotated automatically |

#### Our current state

**No M2M path exists.** All Databricks API calls use the user's bearer token, even for scheduled background work.

#### Recommended approach

**Hybrid:** Keep U2M for interactive setup (Connect ACC, Connect Databricks, Sync Now from UI), and add M2M for scheduled auto-sync.

**Implementation steps for the customer admin:**

1. In Databricks Account Console, create a **Service Principal** named e.g. `cctech-acc-connector-sp`
2. Grant the SP `USE_CATALOG`, `USE_SCHEMA`, `MODIFY` on `acc_catalog.bronze` and `acc_catalog.silver`, plus `WRITE FILES` on `acc_bronze_volume`, plus `CAN MANAGE RUN` on the bronze and silver jobs
3. Configure **Workload Identity Federation** trust policy if their IdP supports it (Azure AD, Okta, etc.) — this eliminates secrets entirely
4. Fall back to OIDC M2M (client ID + secret) if their IdP doesn't support WIF

**Implementation steps in our connector:**

1. Add a "Service Principal Configuration" section in Step 2 (optional — only needed if user enables auto-sync)
2. Store SP credentials encrypted in `dbx_credentials` table (already exists in our schema as part of the PAT fallback)
3. In `_auto_sync_tick()` in [acc-connector/app.py](acc-connector/app.py), use the SP's client credentials grant to obtain a short-lived access token instead of relying on the user's refresh token
4. Token endpoint for OIDC M2M: `POST https://{databricks-host}/oidc/v1/token` with `grant_type=client_credentials&client_id=...&client_secret=...&scope=all-apis`

#### Effort & priority

| Task | Effort | Priority |
|------|--------|----------|
| Add SP-based OIDC M2M for auto-sync only (keep U2M for everything else) | 1 day | Medium — only needed if customers report auto-sync breaking due to user token expiry |
| Add Workload Identity Federation support (preferred over OIDC M2M) | 2 days | Low — most customers won't have WIF set up; OIDC M2M is the practical default |
| Document the SP setup in customer setup guide | 2 hrs | High (when we write the customer setup guide) |

**Validation impact:** M2M is **not directly required** for our specific connector validation because we're a user-facing SaaS product (U2M is our primary flow). However, Databricks reviewers may flag the auto-sync pattern as risky if it relies solely on user refresh tokens. Adding an optional M2M path for auto-sync is a **strong recommendation** and aligns the connector with the framework.

---

## Pillar 3 — Unity Catalog

### What Databricks requires

Source: [Catalog & Metadata](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/catalog-metadata) and [Data Ingestion](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/data-ingest)

Mandatory practices:

| Practice | Requirement |
|----------|-------------|
| **UC semantics** | Use the three-level namespace `<catalog>.<schema>.<table>` and UC interfaces (REST APIs, SDK, drivers) so permissions, audit logs, and metadata are preserved |
| **Least privilege** | Design workflows for non-admin users; document minimum privileges customers must grant |
| **Staging via UC Volumes** | Use UC Volumes for staging files, not direct cloud storage |
| **Managed tables** | Default to UC managed Delta/Iceberg tables. External-only integrations are **not eligible for validation** |
| **Metadata and lineage** | Read and publish schema, tags, and lineage via UC. Use Bring-Your-Own-Lineage (BYOL) API for external lineage |

Customer flow expected:
1. Customer creates the catalog and grants permission to the partner
2. Partner creates schema/volume
3. Partner writes files to UC Volume
4. Partner ingests into managed Delta/Iceberg tables

### Our current state

| Sub-requirement | State | Notes |
|-----------------|-------|-------|
| Three-level namespace | PASS | `acc_catalog.bronze.*`, `acc_catalog.silver.*` |
| UC REST API for catalog/schema/volume creation | PASS | [acc-connector/backend/databricks_client.py](acc-connector/backend/databricks_client.py) `uc_create_catalog`, `uc_create_schema`, `uc_create_volume` |
| Idempotent UC operations | PASS | GET-before-POST in all three UC methods |
| UC Volume for staging | PASS | `/Volumes/acc_catalog/bronze/acc_bronze_volume/` confirmed working |
| Customer creates catalog (least privilege) | PASS | We document `acc_catalog` must be created by customer; we only create schemas/volumes/tables under it |
| Managed Delta tables (vs external) | LIKELY PASS | [acc-connector/notebooks/silver_transform.py](acc-connector/notebooks/silver_transform.py) uses `df.write.format('delta').saveAsTable(...)` which defaults to managed under UC. **Verify explicitly.** |
| Bronze table format | PASS | Delta managed (auto-loaded by [acc-connector/notebooks/bronze_ingestion.py](acc-connector/notebooks/bronze_ingestion.py)) |
| Lineage publishing (BYOL API) | FAIL | Not implemented. Recommended, not mandatory for validation |
| Metadata tagging | FAIL | We don't tag tables with source system info (e.g., `source = 'acc'`, `module = 'issues'`) |
| ACL respect | PASS | We never grant on objects we don't own |

### Fix — Verify managed tables explicitly

Add comments and assertions in [acc-connector/notebooks/silver_transform.py](acc-connector/notebooks/silver_transform.py) to confirm tables are managed:

```python
spark.sql(f'DESCRIBE EXTENDED {silver_table}').filter("col_name = 'Type'").show()
```

If any table shows `EXTERNAL`, switch to managed. **Effort:** 1 hour

### Fix — Add table tags for partner attribution

After creating each silver table, run:

```sql
ALTER TABLE acc_catalog.silver.issues SET TAGS (
  'source' = 'autodesk_acc',
  'partner' = 'cctech',
  'connector_version' = '2.0',
  'module' = 'issues'
);
```

Tags are queryable by customers via `information_schema.tags` and improve discoverability. **Effort:** 1 hour

### Fix — Lineage publishing (BYOL)

Use the Databricks External Lineage API to publish lineage from Autodesk ACC sources to our silver tables:

```
POST /api/2.0/lineage-tracking/external-lineage
{
  "source": { "external": { "url": "https://acc.autodesk.com/projects/{proj}/issues" } },
  "target": { "table_name": "acc_catalog.silver.issues" }
}
```

This shows up in the UC lineage graph for customers. **Effort:** half day. Recommended but not validation-blocking.

---

## Cross-Cutting Requirements

### Guided setup

The framework requires:
> "All integrations must provide a guided and systematic setup process between the partner's platform and the customer's Databricks environment."

**Our state:** PASS. The 3-step wizard (Connect ACC → Connect Databricks → Sync) is exactly this guided experience. Step 4 (Dashboard) added in Upgrade F further improves UX.

### Documentation deliverables

Databricks expects partner docs covering:

- Minimum privileges customer admin must grant (catalog `USE_CATALOG`, schema `CREATE SCHEMA`, volume `WRITE FILES`)
- How to register the OAuth app in Databricks Account Console
- How to obtain the workspace URL
- Troubleshooting common errors

**Our state:** FAIL. No customer-facing setup guide exists yet. **Effort:** 1 day to write `CUSTOMER_SETUP_GUIDE.md`.

### Acceptable Use Policy compliance

Metadata accessed through our integration must comply with the [Databricks Acceptable Use Policy](https://www.databricks.com/legal/acceptable-use-policy).

**Our state:** PASS by default — we only read schema metadata for tables we created.

---

## Recommended (non-mandatory) practices

These are not validation blockers but would harden the connector for production / scale:

| Practice | Source | Effort | Priority |
|----------|--------|--------|----------|
| Auto Loader within Lakeflow SDP for incremental ingestion | [Data ingestion](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/data-ingest) | 2 days | Medium — only if we expect millions of records |
| Lakeflow Spark Declarative Pipelines (SDP) for silver transforms | [Data transformation](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/data-transformation) | 2-3 days | Medium |
| Workload Identity Federation (WIF) for any service-to-service calls | [Access & Authentication](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth) | N/A | Low — we don't have M2M flows |
| Use serverless SQL warehouse for metadata operations | [Catalog & Metadata](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/catalog-metadata) | 0 (already done) | DONE |
| `information_schema` queries instead of `SHOW` | [Catalog & Metadata](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/catalog-metadata) | 1 day | Low |

---

## Compliance Scorecard

| # | Item | Status | Effort | Validation Blocker |
|---|------|--------|--------|--------------------|
| 1 | User-Agent header on all Databricks calls | FAIL | 1 hr | YES |
| 2 | OAuth `state` parameter (Databricks) | FAIL | 1 hr | YES |
| 3 | OAuth `state` parameter (ACC — already noted) | FAIL | 1 hr | NO (ACC side) |
| 4 | PKCE on OAuth U2M | FAIL | 3 hrs | YES |
| 5 | Databricks OIDC vs Azure Entra ID | PARTIAL | 0 (Option A) or 1 day (Option B) | NO if documented as Azure-only |
| 6 | Refresh token persisted encrypted | PASS | — | — |
| 7 | Refresh token rotation (single-use) | PARTIAL | 2 hrs | NO |
| 8 | Token redaction in logs | VERIFY | 2 hrs | NO |
| 9 | Disconnect/re-auth UX | FAIL | half day | NO |
| 9a | M2M path for hourly auto-sync (OIDC M2M with Service Principal) | FAIL | 1 day | NO (recommended) |
| 9b | Workload Identity Federation (WIF) support — preferred over OIDC M2M | FAIL | 2 days | NO (low priority) |
| 10 | UC three-level namespace | PASS | — | — |
| 11 | UC Volumes for staging | PASS | — | — |
| 12 | UC managed tables (vs external) | LIKELY PASS | 1 hr to verify | YES if we're external |
| 13 | UC REST API for DDL (vs raw SQL) | PASS | — | — |
| 14 | Idempotent UC operations | PASS | — | — |
| 15 | Customer creates catalog (least privilege) | PASS | — | — |
| 16 | Table tags (`source`, `partner`, `module`) | FAIL | 1 hr | NO |
| 17 | Lineage via BYOL API | FAIL | half day | NO (recommended) |
| 18 | Guided setup wizard | PASS | — | — |
| 19 | Customer-facing setup documentation | FAIL | 1 day | NO (but expected for partner application) |
| 20 | Auto Loader / SDP for ingestion | FAIL | 2 days | NO (recommended) |
| 21 | Lakeflow SDP for silver transforms | FAIL | 2-3 days | NO (recommended) |

---

## Priority-Ordered Action Plan

To reach Databricks partner validation, complete items in this order:

### Phase 1 — Validation blockers (~1 day total)

| # | Action | File(s) | Effort |
|---|--------|---------|--------|
| 1 | Add `User-Agent: CCTech_ACCConnector/2.0` to all Databricks HTTP calls | [acc-connector/backend/databricks_client.py](acc-connector/backend/databricks_client.py), [acc-connector/app.py](acc-connector/app.py) (OAuth callback) | 1 hr |
| 2 | Add `state` parameter to Databricks OAuth | [acc-connector/app.py](acc-connector/app.py) `/connect/databricks` and `/databricks/callback` | 1 hr |
| 3 | Add PKCE (`code_verifier` + `code_challenge`) to Databricks OAuth | [acc-connector/app.py](acc-connector/app.py) | 3 hrs |
| 4 | Verify `silver_transform.py` produces managed tables (DESCRIBE EXTENDED) | [acc-connector/notebooks/silver_transform.py](acc-connector/notebooks/silver_transform.py) | 1 hr |
| 5 | Add `state` parameter to ACC OAuth (already in PRODUCTION_READINESS_CHECKLIST) | [acc-connector/backend/acc_client.py](acc-connector/backend/acc_client.py) | 1 hr |

### Phase 2 — Strong recommendations (~3 days total)

| # | Action | File(s) | Effort |
|---|--------|---------|--------|
| 6 | Decide Entra ID (Option A) vs Databricks OIDC (Option B); if B, migrate | [acc-connector/app.py](acc-connector/app.py) | 1 day if migrating |
| 7 | Add disconnect/re-auth buttons | [acc-connector/app.py](acc-connector/app.py) | half day |
| 8 | Token redaction audit + helper | All `backend/*.py` | 2 hrs |
| 9 | Add table tags after silver creation | [acc-connector/notebooks/silver_transform.py](acc-connector/notebooks/silver_transform.py) | 1 hr |
| 10 | Add OIDC M2M (Service Principal) path for hourly auto-sync | [acc-connector/app.py](acc-connector/app.py), [acc-connector/backend/state_store.py](acc-connector/backend/state_store.py) | 1 day |

### Phase 3 — Recommended for production (~1 week total)

| # | Action | File(s) | Effort |
|---|--------|---------|--------|
| 11 | Lineage publishing via BYOL API | [acc-connector/backend/sync_service.py](acc-connector/backend/sync_service.py) | half day |
| 12 | Customer setup guide (`CUSTOMER_SETUP_GUIDE.md`) — include SP setup steps | New file | 1 day |
| 13 | Workload Identity Federation (WIF) support — replaces OIDC M2M secrets | [acc-connector/app.py](acc-connector/app.py) | 2 days |
| 14 | Migrate bronze ingestion to Auto Loader within SDP | [acc-connector/notebooks/bronze_ingestion.py](acc-connector/notebooks/bronze_ingestion.py) | 2 days |
| 15 | Migrate silver transforms to Lakeflow SDP | [acc-connector/notebooks/silver_transform.py](acc-connector/notebooks/silver_transform.py) | 2-3 days |

---

## Quick Reference — Framework Pages Cited

- [Integration requirements](https://databrickslabs.github.io/partner-architecture/isv-partners/integration-requirements) — three mandatory pillars
- [Telemetry & Attribution](https://databrickslabs.github.io/partner-architecture/isv-partners/telemetry-attribution) — User-Agent format & validation
- [REST API telemetry](https://databrickslabs.github.io/partner-architecture/isv-partners/telemetry-attribution/rest-apis) — header on REST/SQL Execution/Jobs APIs
- [Access & Authentication](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth) — OAuth method comparison
- [OAuth U2M Implementation](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth/oauth-u2m) — PKCE + state step-by-step
- [OAuth Reference](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/access-auth/oauth-reference) — endpoints, error handling, token TTL
- [Catalog & Metadata](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/catalog-metadata) — UC metadata operations
- [Data Ingestion](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/data-ingest) — Volumes + managed tables + Auto Loader
- [Data Transformation](https://databrickslabs.github.io/partner-architecture/isv-partners/lakehouse-patterns/data-transformation) — medallion + SDP

---

*Last updated: April 28, 2026*
*Next review: After Phase 1 fixes are complete*
