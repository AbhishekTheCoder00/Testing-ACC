# U2M Refresh Token — Manual Integration Guide

Use this document when you **clone the connector to another folder** and want to add or verify U2M refresh logic **by hand**, file by file.

**Scope:** U2M only (browser user OAuth). M2M is out of scope here.

**Line numbers** match the current `acc-connector` tree. After you edit files, line numbers may shift by a few lines — search for the function names if needed.

---

## What you are building

```text
LOGIN (once)                    EVERY API CALL (sync, catalogs, bootstrap)
────────────                    ───────────────────────────────────────────
User → Databricks OAuth         get_valid_dbx_token(user_id)
       ↓                              ↓
save access + refresh           fresh access_token
to SQLite                              ↓
                                 DatabricksClient  OR  WorkspaceClient (SDK)
```

**One auth module** (`databricks_auth.py`). **Two optional HTTP clients** (requests vs SDK).

---

## Quick map — which file does what

| File | Role in U2M refresh |
|------|---------------------|
| `backend/utils/databricks_auth.py` | **Core** — `get_valid_dbx_token()`, refresh HTTP call, factories |
| `backend/repositories/state/token_repository.py` | Save/load encrypted tokens in SQLite |
| `backend/routes/databricks_routes.py` | Login + callback (tokens born here) + catalogs API |
| `backend/routes/bootstrap_routes.py` | Calls `get_valid_dbx_token` before bootstrap |
| `backend/services/sync/sync_orchestrator.py` | Calls `get_valid_dbx_token` before sync |
| `backend/services/zerobus_service.py` | Uses `create_databricks_client_for_user` for API checks |
| `backend/clients/databricks_sdk_adapter.py` | Optional SDK wrapper (Layer 2 only) |
| `tests/test_databricks_auth.py` | Unit tests for refresh behavior |

---

# DECISION GUIDE — Read this first (Patterns 2, 3, 4 in one place)

Use this section only to **decide what to manually integrate** in your clone.

---

## First: understand “SDK does not refresh for you” (very simple)

Many people think: *“I added SDK, so token refresh is handled.”*  
**That is wrong in this connector.**

### Two different jobs

| Job | Who does it in this app | What it does |
|-----|------------------------|--------------|
| **Job A — Keep token fresh** | `get_valid_dbx_token(user_id)` in `databricks_auth.py` | Reads SQLite, checks expiry, calls Databricks with `refresh_token`, saves new tokens |
| **Job B — Call Databricks APIs** | `DatabricksClient` **or** `WorkspaceClient` (SDK) | Uses the token string to list catalogs, run pipelines, etc. |

**SDK only does Job B.** It never does Job A in your app.

### Picture it like a hotel key card

```text
get_valid_dbx_token  = front desk gives you a NEW key if yours expired
SDK / DatabricksClient = the elevator that reads your key

If your key expired:
  • SDK will NOT go to the front desk for you
  • Elevator says "access denied" (403)
  • YOU must call get_valid_dbx_token FIRST, then give SDK the new key
```

### What you pass to SDK

```python
record = get_valid_dbx_token(user_id)          # Job A — YOUR code
wc = create_workspace_client(
    record['workspace_url'],
    record['access_token'],                    # fresh key card
)
wc.catalogs.list()                             # Job B — SDK
```

SDK constructor only receives:

- `host` (workspace URL)
- `token` (one string)

It does **not** receive:

- `user_id`
- `refresh_token`
- your SQLite database

So SDK **cannot** know your user’s refresh token or when to renew it. **Your app must renew first.**

### Same rule for Pattern 2 and Pattern 3

| Pattern | Job A (refresh) | Job B (API calls) |
|---------|-----------------|-------------------|
| **2** | `get_valid_dbx_token` | `DatabricksClient` |
| **3** | `get_valid_dbx_token` | `WorkspaceClient` (SDK) |
| **4** | `get_valid_dbx_token` (once) | Both clients in different files |

**Adding SDK does not remove Job A.**  
**Removing `get_valid_dbx_token` because you added SDK will bring back 403 after ~1 hour.**

---

## One table — advantages & disadvantages (Patterns 2, 3, 4)

| | **Pattern 2** — U2M, no SDK | **Pattern 3** — U2M + SDK only | **Pattern 4** — U2M, both mixed |
|--|----------------------------|--------------------------------|--------------------------------|
| **What you integrate** | `databricks_auth.py` + wire `get_valid_dbx_token` + `DatabricksClient` | Above **plus** SDK on routes you choose | Pattern 2 for sync/bootstrap **plus** optional SDK on catalogs |
| **✅ Advantages** | • Works for **all** connector features (sync, bootstrap, pipelines)<br>• **Easiest** manual clone — one style<br>• Fewer packages<br>• Already in repo today<br>• Fixes 403 after 1 hour | • Official Databricks library<br>• Shorter code for catalogs (`wc.catalogs.list()`)<br>• Good to learn SDK<br>• Still uses same refresh as Pattern 2 | • **Best balance** for this repo<br>• Sync stays safe on `DatabricksClient`<br>• Try SDK on one route only<br>• **One** refresh module for everything<br>• Production-ready |
| **❌ Disadvantages** | • Long `databricks_client.py`<br>• Manual HTTP code<br>• No SDK typed models | • **Cannot run whole app** on SDK alone<br>• Extra package<br>• **Still must add** `get_valid_dbx_token` (SDK does not replace it)<br>• Convert SDK objects to dicts for UI | • Two HTTP styles to remember<br>• Slightly more files to touch<br>• “sync = no SDK, catalogs = maybe SDK” |
| **Manual effort** | Low | Medium | Medium |
| **Works for sync?** | ✅ Yes | ❌ No (not fully) | ✅ Yes |
| **Works for catalogs?** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Need `get_valid_dbx_token`?** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Need SDK package?** | ❌ No | ✅ Yes | Optional |

---

## Which pattern should YOU manually integrate?

Answer these questions in order:

```text
1. Is this your first clone / you want simplest learning?
   YES → Pattern 2  ✅ STOP HERE

2. Do you need sync + bootstrap + pipelines to work?
   YES → You MUST include Pattern 2 (at minimum)

3. Do you also want to practice SDK on catalog list only?
   YES → Pattern 4  ✅ RECOMMENDED
   NO  → Stay on Pattern 2  ✅ ALSO FINE

4. Do you want SDK everywhere and no DatabricksClient?
   → Pattern 3 alone  ❌ DO NOT — sync will break
```

### Quick pick (copy this)

| Your goal | Integrate this |
|-----------|----------------|
| “I just want refresh to work, simplest clone” | **Pattern 2** |
| “I want refresh + try SDK on catalogs” | **Pattern 4** |
| “I want SDK only, no DatabricksClient” | **Don’t** — use Pattern 4 instead |
| “I’m not sure” | **Pattern 2** first, add SDK later (becomes Pattern 4) |

### What every pattern must include (non-negotiable)

No matter 2, 3, or 4 — always manually add/keep:

1. `backend/utils/databricks_auth.py` with `get_valid_dbx_token`
2. `save_dbx_tokens` saving **refresh_token**
3. Login callback (line ~176) saving refresh token
4. `get_valid_dbx_token` before **any** Databricks API call

SDK is **extra** on top. Refresh is **required** for all.

---

## Pattern summary in one sentence each

| Pattern | One sentence |
|---------|--------------|
| **2** | Refresh with `get_valid_dbx_token`, then always use `DatabricksClient`. |
| **3** | Same refresh, then use SDK — but only for small features, not whole app. |
| **4** | Same refresh once; `DatabricksClient` for sync, SDK optional for catalogs. |

**Parts 2, 3, 4 below** have file names and line numbers for each pattern.  
**Advantages/disadvantages** — use the table above only (not repeated below).

---

# Part 1 — U2M refresh core (what to add / remove)

If your clone **does not** have refresh yet, add these pieces in order. If your clone **already matches** this repo, use Part 1 as a checklist.

---

## Step 1.1 — Create or verify `databricks_auth.py`

**File:** `backend/utils/databricks_auth.py`  
**Action:** **ADD** entire file (or verify `get_valid_dbx_token` exists at ~line 102).

**Minimum U2M refresh function** (the heart of the fix):

```python
def get_valid_dbx_token(user_id: str) -> dict:
    record = db.get_dbx_tokens(user_id)
    if not record:
        raise RuntimeError('Databricks not connected — complete OAuth first')

    if time.time() < record['expires_at'] - TOKEN_REFRESH_BUFFER_SEC:
        return record   # still valid — no HTTP call

    refresh_token = record.get('refresh_token')
    if not refresh_token:
        raise RuntimeError('Databricks refresh token missing — user must re-authenticate')

    # POST grant_type=refresh_token to Databricks token_url
    # ...
    db.save_dbx_tokens(user_id, workspace_url, new_access, new_refresh, expires_in, ...)
    return db.get_dbx_tokens(user_id)
```

**Also add** at bottom of same file (~lines 224–229):

```python
def create_databricks_client_for_user(user_id: str):
    from backend.clients.databricks_client import DatabricksClient
    record = get_valid_dbx_token(user_id)
    return DatabricksClient(record['workspace_url'], record['access_token'])
```

**Remove / do not use:**

| Bad pattern | Why |
|-------------|-----|
| `update_dbx_access_token()` after refresh | Saves access token only — **loses rotated refresh_token** → 403 later |
| Reading `db.get_dbx_tokens()` and calling APIs directly | No refresh — 403 after ~60 min |

---

## Step 1.2 — Token storage must save refresh_token

**File:** `backend/repositories/state/token_repository.py`  
**Lines:** 74–92 (`save_dbx_tokens`), 95–106 (`get_dbx_tokens`)

**Keep:** `save_dbx_tokens` must accept and persist `refresh_token` (encrypted).

**Do not remove** `refresh_token` column from the `ON CONFLICT` update (line 87).

**Legacy — do not call for Databricks refresh:**

```python
# token_repository.py ~line 109
def update_dbx_access_token(...)  # access-only update — WRONG for U2M refresh rotation
```

---

## Step 1.3 — Login callback must save refresh_token at connect time

**File:** `backend/routes/databricks_routes.py`

| Location | Line | What to keep |
|----------|------|--------------|
| OAuth scope | 12 in `databricks_auth.py` | `offline_access` in scope helps get refresh_token |
| Callback reads refresh | **158** | `refresh_token = tokens.get('refresh_token')` |
| Save both tokens | **176** | `db.save_dbx_tokens(uid, workspace_url, access_token, refresh_token, expires_in, ...)` |

**Remove nothing** in callback if refresh already works — this is where tokens are **born**.

---

## Step 1.4 — Wire refresh at every **API call** site

These are the places that **call Databricks APIs**. Each must use `get_valid_dbx_token` or `create_databricks_client_for_user` — **not** raw `db.get_dbx_tokens()`.

### A) Bootstrap

**File:** `backend/routes/bootstrap_routes.py`

| Line | Current (correct) | If your clone has old code |
|------|-------------------|------------------------------|
| **28** | `from backend.utils.databricks_auth import get_valid_dbx_token` | **ADD** import |
| **47** | `get_valid_dbx_token(uid)` | **REPLACE** `db.get_dbx_tokens(uid)` check |
| **53–54** | `fresh = get_valid_dbx_token(uid)` then pass token to bootstrap | **REPLACE** `db.get_dbx_tokens` + stale `access_token` |

**Remove (old pattern):**

```python
# REMOVE — no refresh
tok = db.get_dbx_tokens(uid)
bs.run_bootstrap(uid, tok['workspace_url'], tok['access_token'], catalog_name)
```

**Add (correct):**

```python
fresh = get_valid_dbx_token(uid)
bs.run_bootstrap(uid, fresh['workspace_url'], fresh['access_token'], catalog_name)
```

---

### B) Sync orchestrator

**File:** `backend/services/sync/sync_orchestrator.py`

| Line | Current (correct) | Change if needed |
|------|-------------------|------------------|
| **9** | `from backend.utils.databricks_auth import get_valid_dbx_token` | **ADD** import |
| **98** | `dbx_tok = get_valid_dbx_token(user_id)` | **REPLACE** `db.get_dbx_tokens(user_id)` |
| **140–148** | Uses `dbx_tok['workspace_url']` and `dbx_tok['access_token']` | Keep — token is already fresh |

**Remove (old pattern):**

```python
# REMOVE
dbx_tok = db.get_dbx_tokens(user_id)
if not dbx_tok:
    raise RuntimeError(...)
dbx = DatabricksClient(dbx_tok['workspace_url'], dbx_tok['access_token'])
```

**Add (correct):**

```python
dbx_tok = get_valid_dbx_token(user_id)
workspace_url = dbx_tok['workspace_url']
pat = dbx_tok['access_token']
dbx = DatabricksClient(workspace_url, pat)
```

---

### C) Catalogs route

**File:** `backend/routes/databricks_routes.py`

| Line | Current (correct — no SDK) | Notes |
|------|----------------------------|-------|
| **38–43** | imports `create_databricks_client_for_user` | Factory calls `get_valid_dbx_token` inside |
| **204–205** | `client = create_databricks_client_for_user(uid)` | Correct for **without SDK** |

**Remove (old pattern):**

```python
# REMOVE
tok = db.get_dbx_tokens(uid)
client = DatabricksClient(tok['workspace_url'], tok['access_token'])
```

---

### D) Zerobus service

**File:** `backend/services/zerobus_service.py`

| Line | Usage | OK? |
|------|-------|-----|
| **135** | `db.get_dbx_tokens` — only checks "is connected?" | OK for UI check |
| **171** | `create_databricks_client_for_user(user_id)` — real API calls | Correct |

**Do not replace line 135** unless you need refreshed workspace URL for display. **Line 171 must stay** on the factory or `get_valid_dbx_token`.

---

### E) Places that can keep `db.get_dbx_tokens` (display only — no API)

| File | Line | Why OK |
|------|------|--------|
| `backend/routes/acc_routes.py` | 54 | Status JSON only (`dbx_token_present`, workspace URL) |
| `backend/routes/dashboard_routes.py` | 71 | Dashboard links — no Databricks API call |

---

## Step 1.5 — Tests

**File:** `tests/test_databricks_auth.py`  
**Action:** **ADD** file (or copy from this repo).

Run:

```bash
cd acc-connector
python -m unittest tests.test_databricks_auth -v
```

---

## Step 1.6 — Environment variables

**File:** `.env` (not committed)

```env
DATABRICKS_CLIENT_ID=...
DATABRICKS_CLIENT_SECRET=...
DATABRICKS_REDIRECT_URI=http://localhost:8000/databricks/callback
DATABRICKS_OAUTH_SCOPE=all-apis offline_access
```

`offline_access` increases chance Databricks returns a `refresh_token` at login.

---

# Part 2 — U2M without SDK (pattern 2)

**Best for:** sync, bootstrap, pipelines, zerobus — everything that uses `DatabricksClient` today.

## Mental model

```text
get_valid_dbx_token(user_id)  OR  create_databricks_client_for_user(user_id)
              ↓
       DatabricksClient(workspace_url, access_token)
              ↓
    client.uc_list_catalogs() / client.start_pipeline_update() / ...
```

## Code patterns (copy-paste)

### Pattern 2A — Factory (shortest)

```python
from backend.utils.databricks_auth import create_databricks_client_for_user

client = create_databricks_client_for_user(user_id)
raw = client.uc_list_catalogs()
```

**Used today at:** `databricks_routes.py` line **204**, `zerobus_service.py` line **171**.

### Pattern 2B — Explicit token then client (sync style)

```python
from backend.utils.databricks_auth import get_valid_dbx_token
from backend.clients.databricks_client import DatabricksClient

dbx_tok = get_valid_dbx_token(user_id)
dbx = DatabricksClient(dbx_tok['workspace_url'], dbx_tok['access_token'])
```

**Used today at:** `sync_orchestrator.py` lines **98**, **140–148**.

### Pattern 2C — Pass token to a service (bootstrap style)

```python
from backend.utils.databricks_auth import get_valid_dbx_token

fresh = get_valid_dbx_token(uid)
bs.run_bootstrap(uid, fresh['workspace_url'], fresh['access_token'], catalog_name)
```

**Used today at:** `bootstrap_routes.py` lines **53–54**.

## What to remove for pattern 2

| Remove | Replace with |
|--------|--------------|
| `db.get_dbx_tokens()` before API calls | `get_valid_dbx_token()` or `create_databricks_client_for_user()` |
| `update_dbx_access_token()` in refresh flow | `db.save_dbx_tokens(..., new_refresh, ...)` inside `get_valid_dbx_token` |

> **Pros / cons:** see **DECISION GUIDE** at the top of this doc.

---

# Part 3 — U2M with SDK (pattern 3)

**Best for:** catalog listing, new UC/SQL features, trying official Databricks client.

## Mental model

```text
get_valid_dbx_token(user_id)     ← REQUIRED (auth layer — same as pattern 2)
              ↓
create_workspace_client(url, token)   ← SDK layer
              ↓
wc.catalogs.list()
```

**SDK does not replace refresh.** You always run Layer 1 first.

## File to add or verify

**File:** `backend/clients/databricks_sdk_adapter.py` (~34 lines)

```python
def create_workspace_client(host: str, access_token: str) -> WorkspaceClient:
    from databricks.sdk import WorkspaceClient
    return WorkspaceClient(host=host.rstrip('/'), token=access_token)
```

**Dependency:** `requirements.txt` — `databricks-sdk>=0.20.0,<1.0`

## Exact change — catalogs route only

**File:** `backend/routes/databricks_routes.py`

### ADD imports (~line 38–44)

```python
from backend.utils.databricks_auth import (
    DBX_OAUTH_SCOPE,
    _detect_cloud_provider,
    _resolve_databricks_oidc_endpoints,
    get_valid_dbx_token,                    # ADD
    create_databricks_client_for_user,      # REMOVE if catalogs is your only U2M SDK site
)
from backend.clients.databricks_sdk_adapter import create_workspace_client  # ADD
```

### REMOVE lines 204–205

```python
        client = create_databricks_client_for_user(uid)
        raw = client.uc_list_catalogs()
```

### ADD in place of removed lines

```python
        record = get_valid_dbx_token(uid)
        wc = create_workspace_client(record['workspace_url'], record['access_token'])
        raw = [
            {
                'name':         c.name,
                'comment':      c.comment or '',
                'catalog_type': c.catalog_type or '',
                'storage_root': c.storage_root or '',
            }
            for c in wc.catalogs.list()
            if c.name
        ]
```

### KEEP lines 206–248

The `for c in raw:` loop, sorting, and `jsonify` — unchanged.

## What NOT to remove when adding SDK

| Keep | Why |
|------|-----|
| `get_valid_dbx_token` in bootstrap/sync | They still need refresh |
| `databricks_auth.py` | SDK has no SQLite refresh |
| `DatabricksClient` in sync | Pipelines not migrated to SDK |
| Login callback `save_dbx_tokens` | Refresh token born at login |

> **Pros / cons + why SDK still needs `get_valid_dbx_token`:** see **DECISION GUIDE** at the top.

---

# Part 4 — Combination: U2M with AND without SDK (pattern 4)

This is the **recommended setup for this connector** when learning or running in production.

## Architecture

```text
                    backend/utils/databricks_auth.py
                    get_valid_dbx_token(user_id)  ← ONE auth path
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   bootstrap_routes    sync_orchestrator    databricks_routes
   Pattern 2C           Pattern 2B           Pattern 2A or 3
   (token pass)         DatabricksClient     catalogs: SDK optional
          │                   │                   │
          └───────────────────┴───────────────────┘
                         Databricks APIs
```

## File-by-file combination table

| File | Pattern | Line(s) | Code |
|------|---------|---------|------|
| `databricks_auth.py` | Core | 102–163, 224–229 | `get_valid_dbx_token`, `create_databricks_client_for_user` |
| `databricks_routes.py` (callback) | Login | 176 | `save_dbx_tokens(..., refresh_token, ...)` |
| `databricks_routes.py` (catalogs) | **2A or 3** | 204–205 | Factory **or** SDK — pick one |
| `bootstrap_routes.py` | 2C | 47, 53–54 | `get_valid_dbx_token` only |
| `sync_orchestrator.py` | 2B | 98, 148 | `get_valid_dbx_token` + `DatabricksClient` |
| `zerobus_service.py` | 2A | 171 | `create_databricks_client_for_user` |
| `databricks_sdk_adapter.py` | 3 helper | all | Only if catalogs uses SDK |

## What you maintain in combination mode

| Count | What |
|-------|------|
| **1** | Auth module (`databricks_auth.py`) |
| **1** | Primary HTTP client (`DatabricksClient`) for sync/bootstrap |
| **0–1** | SDK adapter for optional routes |
| **Not 2** | You do **not** duplicate refresh logic |

## Manual clone checklist (combination)

```text
[ ] databricks_auth.py present with get_valid_dbx_token
[ ] save_dbx_tokens stores refresh_token
[ ] callback line 176 saves refresh_token
[ ] bootstrap uses get_valid_dbx_token (lines 47, 53)
[ ] sync uses get_valid_dbx_token (line 98)
[ ] catalogs uses create_databricks_client_for_user OR SDK pattern
[ ] no API site uses db.get_dbx_tokens() without refresh
[ ] tests/test_databricks_auth.py passes
[ ] (optional) SDK only on catalogs
```

> **Pros / cons:** see **DECISION GUIDE** at the top of this doc.

---

# Part 5 — Which is best for YOUR code? (2 vs 3 vs 4)

> Full comparison table is in **DECISION GUIDE** at the top. This section is a short recap.

## Short answer

| Choice | Verdict for this connector |
|--------|---------------------------|
| **Pattern 2 only** (U2M, no SDK) | **Best default** — matches 95% of the app today |
| **Pattern 3 only** (U2M, SDK everywhere) | **Not feasible** — sync needs pipelines/files via `DatabricksClient` |
| **Pattern 4** (combination) | **Best long-term** — Pattern 2 for sync + optional SDK for catalogs |

## Why Pattern 2 is the best fit today

1. **`sync_orchestrator.py`** calls `pipeline_has_active_update`, volume paths, workflow triggers — implemented in `databricks_client.py` (852 lines), not in your SDK snippet.
2. **`bootstrap_service`** expects a bearer token and uses `DatabricksClient` patterns throughout provisioning.
3. **Refresh fix is independent of SDK** — Pattern 2 already solves the 403-after-1-hour bug.
4. **Less moving parts** when you clone and learn manually.

## Why add Pattern 3 (SDK) at all?

- Good **learning exercise** on one route (`/databricks/catalogs`).
- Official client for **new** Unity Catalog / SQL APIs you add later.
- Does **not** replace Pattern 2 for sync.

## Why Pattern 4 is the practical “best”

- **One** `get_valid_dbx_token` — no confusion.
- Sync/bootstrap stay stable on `DatabricksClient`.
- You can try SDK on catalogs without breaking sync.
- Matches how `databricks_sdk_adapter.py` comment describes “partial adoption”.

## Decision flowchart

```text
Are you calling Databricks APIs for sync/pipelines/bootstrap?
  YES → Pattern 2 (DatabricksClient + get_valid_dbx_token)
  NO  → Is it only catalog list / new UC read API?
          YES → Pattern 3 OK on that route only (still get_valid_dbx_token first)
          NO  → Pattern 2 unless you implement SDK equivalent

Do you want easiest manual clone?
  → Pattern 2 everywhere (skip SDK until comfortable)

Do you want to learn both?
  → Pattern 4: keep Pattern 2 in sync/bootstrap, add Pattern 3 on catalogs only
```

---

# Appendix A — Before / after summary (old clone → fixed clone)

| Site | BEFORE (broken) | AFTER (fixed) |
|------|-----------------|---------------|
| Refresh logic | Missing or `update_dbx_access_token` only | `get_valid_dbx_token` in `databricks_auth.py` |
| Sync ~98 | `db.get_dbx_tokens(user_id)` | `get_valid_dbx_token(user_id)` |
| Bootstrap ~53 | stale token from DB | `get_valid_dbx_token(uid)` |
| Catalogs ~204 | `DatabricksClient` with stale token | `create_databricks_client_for_user(uid)` |
| Catalogs (SDK option) | same | `get_valid_dbx_token` + `create_workspace_client` |

---

# Appendix B — Order to add code manually in a fresh clone

1. `token_repository.py` — `save_dbx_tokens` / `get_dbx_tokens` with refresh_token  
2. `databricks_auth.py` — full file  
3. `databricks_routes.py` — callback save (line 176)  
4. `bootstrap_routes.py` — wire `get_valid_dbx_token`  
5. `sync_orchestrator.py` — wire `get_valid_dbx_token`  
6. `databricks_routes.py` — catalogs use factory (Pattern 2)  
7. `tests/test_databricks_auth.py` — run tests  
8. *(Optional)* `databricks_sdk_adapter.py` + catalogs SDK swap (Pattern 3)  

---

# Appendix C — Verify refresh works

1. Connect Databricks in UI (login once).  
2. Check SQLite has `refresh_token` in `dbx_tokens`.  
3. Set `expires_at` in DB to past time (or wait 60+ min).  
4. Trigger sync or `/databricks/catalogs`.  
5. Logs should show: `Refreshing Databricks access token for user ...`  
6. Sync should succeed without browser re-login.

---

**Related docs:** `docs/DATABRICKS_AUTH.md`, `docs/AUTH_U2M_M2M_SDK_INTEGRATION.md`
