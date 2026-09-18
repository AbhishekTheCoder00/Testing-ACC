# ACC Connector v2 — Production Readiness Checklist

**Assessed:** April 2026
**Current state:** POC / Internal Demo quality
**Target:** Multi-tenant production deployment on Azure App Service

> Use this document to track what needs to be built before the connector can be used in production with real users and real ACC data.

---

## Summary Verdict

| Layer | POC | Production |
|-------|-----|------------|
| Auth & Identity | Single hardcoded user | Multi-user (Azure AD / Entra ID) |
| Data store | SQLite | PostgreSQL / Azure SQL |
| Background jobs | Daemon threads | Celery + Redis / Azure Service Bus |
| Security | Basics done | CSRF, OAuth state, rate limiting needed |
| Observability | stdout logs | Azure Monitor / App Insights |
| Resilience | None | Retry, dead-letter, health checks |

---

## CRITICAL — Must fix before any real users

### 1. Multi-User Authentication
- **Problem:** `DEMO_USER_ID = 'user_001'` is hardcoded in `app.py` line 31. Every request uses the same identity, meaning all users share one set of credentials, tokens, and sync state.
- **Fix:** Integrate Azure Active Directory (Entra ID) login using `msal` or `flask-login` + Azure AD. Derive `user_id` from the authenticated principal (e.g., AAD object ID or email).
- **Files to change:** `app.py` (all routes), `backend/state_store.py` (no schema changes needed — `user_id` column already exists)
- **Effort:** Medium (2–3 days)

---

### 2. OAuth State Parameter (Security Bug)
- **Problem:** The Autodesk OAuth redirect URL in `backend/acc_client.py` → `get_auth_url()` does not include a `state` parameter. Without it, an attacker can forge a callback request and hijack the token exchange (CSRF on OAuth).
- **Fix:** Generate a cryptographically random `state` value, store it in the user's session before redirecting, and verify it matches on `/callback`.
  ```python
  import secrets
  state = secrets.token_urlsafe(32)
  session['oauth_state'] = state
  # append &state={state} to the auth URL
  # in /callback: assert request.args['state'] == session.pop('oauth_state')
  ```
- **Files to change:** `backend/acc_client.py`, `app.py` (`/connect/acc` and `/callback` routes)
- **Effort:** Low (2–3 hours)

---

### 3. CSRF Protection on All POST Routes
- **Problem:** Routes `/save-acc-config`, `/connect/databricks`, and `/sync` accept POST requests with no CSRF token. A malicious page can silently trigger a sync or overwrite Databricks credentials.
- **Fix:** Add `Flask-WTF` and enable `CSRFProtect`. For JSON APIs, validate a CSRF token header (`X-CSRFToken`).
  ```bash
  pip install Flask-WTF
  ```
  ```python
  from flask_wtf.csrf import CSRFProtect
  csrf = CSRFProtect(app)
  ```
- **Files to change:** `app.py`, `requirements.txt`
- **Effort:** Low (half a day)

---

### 4. Replace SQLite with PostgreSQL / Azure SQL
- **Problem:** SQLite has no concurrent write support. With multiple users or parallel sync runs, you will hit `database is locked` errors. SQLite is also not durable in Azure App Service (ephemeral filesystem).
- **Fix:** Swap `sqlite3` for `SQLAlchemy` with a PostgreSQL or Azure SQL backend. The schema in `state_store.py` maps directly — no logic changes needed, only the connection layer.
  ```bash
  pip install sqlalchemy psycopg2-binary
  # or: pip install pyodbc for Azure SQL
  ```
- **New env var:** `DATABASE_URL=postgresql://user:pass@host/dbname`
- **Files to change:** `backend/state_store.py` (replace `sqlite3` calls with SQLAlchemy session)
- **Effort:** Medium (1–2 days)

---

## HIGH — Fix before going live with more than 1–2 users

### 5. Replace Daemon Threads with a Proper Task Queue
- **Problem:** Bootstrap and sync run in `daemon=True` threads. Daemon threads are killed silently when the Flask process restarts (deploys, crashes, App Service restarts). Mid-sync the `sync_run` record stays stuck in "running" forever with no way to recover.
- **Fix (Option A — simpler):** Use **Celery + Redis** (Azure Cache for Redis).
  ```bash
  pip install celery[redis]
  ```
- **Fix (Option B — Azure-native):** Use **Azure Service Bus** to enqueue sync tasks and a separate worker process to consume them.
- **Files to change:** `app.py` (routes → enqueue task instead of starting thread), new `tasks.py` (Celery task definitions), `requirements.txt`
- **Effort:** High (3–5 days)

---

### 6. Rate Limiting
- **Problem:** No rate limiting on any endpoint. Anyone who can reach the app can:
  - Spam `/sync` and trigger unlimited Databricks job runs (cost explosion)
  - Spam `/connect/databricks` and exhaust ACC/Databricks API quotas
- **Fix:** Add `Flask-Limiter`.
  ```bash
  pip install Flask-Limiter
  ```
  ```python
  from flask_limiter import Limiter
  limiter = Limiter(app, key_func=get_remote_address)

  @app.route('/sync', methods=['POST'])
  @limiter.limit('5 per minute')
  def start_sync(): ...
  ```
- **Files to change:** `app.py`, `requirements.txt`
- **Effort:** Low (half a day)

---

### 7. Health Check Endpoint
- **Problem:** No `/health` or `/ping` route. Azure App Service, load balancers, and uptime monitors need this to determine if the app is alive. Without it, a crashed app may not be detected.
- **Fix:** Add a lightweight health endpoint that checks DB connectivity.
  ```python
  @app.route('/health')
  def health():
      try:
          db.init_db()  # or a simple SELECT 1
          return jsonify({'status': 'ok'}), 200
      except Exception as e:
          return jsonify({'status': 'error', 'detail': str(e)}), 500
  ```
- **Files to change:** `app.py`
- **Effort:** Very low (1 hour)

---

### 8. Retry Logic on Sync / Bootstrap Failure
- **Problem:** If a Databricks job fails mid-sync, the run is marked `failed` and the user must manually retry. There is no automatic retry with backoff, no dead-letter handling, and no partial recovery (e.g., re-trigger only the failed job).
- **Fix:** Add configurable retry attempts with exponential backoff in `sync_service.py` and `bootstrap.py`. Store last failed step in SQLite so retry resumes from where it left off rather than restarting from step 1.
- **Effort:** Medium (1–2 days)

---

## MEDIUM — Fix before scaling beyond a pilot team

### 9. Structured Logging + Azure Application Insights
- **Problem:** All logs go to stdout with `logging.basicConfig`. In Azure App Service there is no log aggregation, search, or alerting.
- **Fix:** Add `opencensus-ext-azure` or `azure-monitor-opentelemetry` for structured logs and traces sent to Azure Application Insights.
  ```bash
  pip install azure-monitor-opentelemetry
  ```
  ```python
  from azure.monitor.opentelemetry import configure_azure_monitor
  configure_azure_monitor(connection_string=os.getenv('APPLICATIONINSIGHTS_CONNECTION_STRING'))
  ```
- **New env var:** `APPLICATIONINSIGHTS_CONNECTION_STRING`
- **Files to change:** `app.py`, `requirements.txt`
- **Effort:** Low (half a day)

---

### 10. Secrets in Azure Key Vault
- **Problem:** Secrets (`SECRET_KEY`, `APS_CLIENT_SECRET`, Databricks PAT) live in environment variables or `.env`. On App Service this means they appear in plain text in the portal Configuration blade.
- **Fix:** Store secrets in **Azure Key Vault** and reference them as Key Vault references in App Service configuration, or use the `azure-keyvault-secrets` SDK at startup.
  ```bash
  pip install azure-identity azure-keyvault-secrets
  ```
- **Effort:** Medium (1 day, including Key Vault setup)

---

### 11. Sync Concurrency Guard
- **Problem:** If a user clicks "Sync Now" twice quickly, two sync threads start simultaneously, both fetching the same ACC data, writing duplicate JSON files to the UC Volume, and triggering duplicate job runs.
- **Fix:** Check `get_latest_sync_run()` before starting a new run — if state is `running`, reject with HTTP 409 Conflict.
  ```python
  existing = db.get_latest_sync_run(user_id)
  if existing and existing['state'] in ('running', 'pending', 'fetching_issues', ...):
      return jsonify({'error': 'Sync already in progress'}), 409
  ```
- **Files to change:** `app.py` (`/sync` route)
- **Effort:** Very low (1 hour)

---

### 12. Input Validation on All API Routes
- **Problem:** Routes like `/save-acc-config` and `/connect/databricks` do minimal validation. Malformed or unexpectedly long strings could cause errors or unexpected behavior.
- **Fix:** Add `marshmallow` or `pydantic` schemas to validate and sanitize all incoming JSON bodies before processing.
  ```bash
  pip install marshmallow
  ```
- **Files to change:** `app.py`, new `schemas.py`
- **Effort:** Low–Medium (1 day)

---

## LOW — Polish before public release

### 13. HTTPS Enforcement
- **Status:** Handled by Azure App Service automatically when deployed — but enforce it explicitly.
- **Fix:** Add HTTPS redirect in `app.py` and set `SESSION_COOKIE_SECURE = True`.
  ```python
  app.config['SESSION_COOKIE_SECURE'] = True
  app.config['SESSION_COOKIE_HTTPONLY'] = True
  app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
  ```

---

### 14. Token Expiry Edge Cases
- **Problem:** If a user's ACC refresh token expires (they haven't synced in 14+ days), the connector silently fails with a confusing error. The UI shows a generic "sync failed" message.
- **Fix:** Detect refresh token expiry specifically and surface a clear "Re-connect ACC" message in the UI.

---

### 15. Databricks Cluster Node Type Portability
- **Problem:** `bootstrap.py` hardcodes `node_type_id: Standard_DS3_v2`. This is an Azure-specific VM type and will fail in AWS or GCP Databricks deployments.
- **Fix:** Make `node_type_id` a configurable environment variable with `Standard_DS3_v2` as the default.
  ```python
  node_type_id = os.getenv('DBX_NODE_TYPE', 'Standard_DS3_v2')
  ```

---

### 16. End-to-End Tests
- **Problem:** No automated tests exist. No way to verify the connector works after a code change without manually clicking through the UI.
- **Fix:** Add `pytest` tests for:
  - `state_store.py` — encrypt/decrypt round-trip, watermark update, sync run state machine
  - `acc_client.py` — mock OAuth responses
  - `bootstrap.py` — mock Databricks API responses, verify all 10 steps execute in order
  - `sync_service.py` — mock ACC fetch + Files API write, verify watermark updated correctly

---

## Prioritised Action Plan

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| P0 | Multi-user auth (AAD) | Medium | Blocks all real use |
| P0 | OAuth state parameter | Low | Security bug |
| P0 | CSRF protection | Low | Security bug |
| P0 | PostgreSQL / Azure SQL | Medium | Data durability |
| P1 | Task queue (Celery) | High | Reliability |
| P1 | Rate limiting | Low | Cost protection |
| P1 | Health check endpoint | Very low | Ops requirement |
| P1 | Sync concurrency guard | Very low | Data integrity |
| P2 | Application Insights | Low | Observability |
| P2 | Azure Key Vault | Medium | Secrets hygiene |
| P2 | Retry logic | Medium | Resilience |
| P2 | Input validation | Low–Medium | Robustness |
| P3 | HTTPS / cookie flags | Very low | Security hardening |
| P3 | Token expiry UX | Low | User experience |
| P3 | Configurable node type | Very low | Portability |
| P3 | End-to-end tests | Medium | Maintainability |

---

## Scaling Upgrades — Implementation Plan (B, F, G)

These upgrades move the POC towards a production-ready, multi-user application. Upgrade A (Databricks OAuth) is already complete.

### Upgrade B — Multi-User Support

**Goal:** Replace the hardcoded `DEMO_USER_ID = 'user_001'` with a real user identity derived from the Autodesk profile.

**Implementation:**

| Step | Detail |
|------|--------|
| 1 | After ACC OAuth callback, call `GET https://developer.api.autodesk.com/userprofile/v1/users/@me` with the 3-legged token |
| 2 | Extract the user's email (e.g. `prabal.singh@autodesk.com`) and use it as `user_id` |
| 3 | Store `user_id` in Flask `session['user_id']` so all subsequent requests use it |
| 4 | Replace every reference to `DEMO_USER_ID` in `app.py` with `session['user_id']` |
| 5 | Add a login guard: if `session['user_id']` is not set, redirect to ACC OAuth |

**Files to change:**
- `app.py` — all routes that reference `DEMO_USER_ID`
- `backend/acc_client.py` — add `fetch_user_profile(token)` function

**Schema impact:** None — all `connector.db` tables already have a `user_id` column.

**Effort:** Low (3–4 hours)

**Status:** PENDING

---

### Upgrade F — Better UI / Business Dashboard

**Goal:** Add a Dashboard panel (Step 4 in sidebar) showing sync health at a glance, designed for business users.

**Implementation:**

| Component | Detail |
|-----------|--------|
| Dashboard panel | New sidebar item "Dashboard" unlocked after first successful sync |
| Sync history table | Last 10 sync runs with timestamp, status, record counts, duration |
| Record count cards | Total Issues / RFIs / Cost synced (lifetime) |
| Bar chart | Records per module per sync run — uses Chart.js via CDN (no npm install) |
| Last sync time | Per-module timestamp showing when each data type was last synced |
| Business-friendly | No technical jargon (no "bronze", "silver", "bootstrap" — use "Data imported", "Ready to analyse") |

**Files to change:**
- `app.py` — add Dashboard HTML panel, new `/dashboard/data` API route
- `backend/state_store.py` — add `get_sync_history(user_id, limit)` and `get_record_totals(user_id)` queries

**Dependencies:** Chart.js loaded from CDN (`https://cdn.jsdelivr.net/npm/chart.js`)

**Effort:** Medium (1 day)

**Status:** PENDING

---

### Upgrade G — Alerts, Logging & Monitoring

**Goal:** Add operational observability — Teams alerts on failure, structured logging, and a health endpoint.

**Implementation:**

| Component | Detail |
|-----------|--------|
| **Teams webhook alert** | On sync failure, POST to `TEAMS_WEBHOOK_URL` (from `.env`) with run_id, user_id, error message, timestamp. If env var not set, skip silently. |
| **Structured logging** | On every sync completion, log a JSON line with: `run_id`, `user_id`, `module`, `record_count`, `duration_sec`, `status`. Use Python `logging` with a JSON formatter. |
| **`/health` endpoint** | Returns JSON: `{ status, last_sync_status, last_sync_time, db_size_kb, uptime_sec }`. Used by load balancers and uptime monitors. |

**Files to change:**
- `backend/sync_service.py` — add Teams alert call in `except` block, add structured log on completion
- `app.py` — add `GET /health` route
- `.env` — add `TEAMS_WEBHOOK_URL` (optional)

**New env vars:**

| Variable | Required | Purpose |
|----------|----------|---------|
| `TEAMS_WEBHOOK_URL` | No | Microsoft Teams incoming webhook URL for failure alerts |

**Effort:** Low–Medium (half a day)

**Status:** PENDING

---

### Build Order & Dependencies

```
A (Databricks OAuth)  ✅ DONE
    ↓
B (Multi-user)        ← Next — required before F and G make sense
    ↓
F (Dashboard UI)      ← Needs multi-user to show per-user data
    ↓
G (Alerts/Monitoring) ← Final polish — Teams alerts, /health, structured logs
```

---

## What IS Production-Ready Already

These are done correctly and do not need changes:

- Fernet encryption for ACC tokens and Databricks PAT at rest in SQLite
- `SECRET_KEY` loaded from environment variable — never hardcoded
- `secure_filename()` used for any file handling
- Server-side signed Flask sessions (not `localStorage`)
- Incremental watermark sync — only fetches records updated since last run
- ACC data never written to connector disk — in-memory only
- Bootstrap idempotency — safe to re-run with `IF NOT EXISTS` and `get_or_create`
- Workspace URL sanitization via `urlparse` — strips browser path garbage
- Notebook deployed to user home folder (`/Users/{email}/acc/v1/`) — avoids permission issues
- Unity Catalog Volume for storage — no storage connection string needed

---

*Last updated: April 28, 2026*
