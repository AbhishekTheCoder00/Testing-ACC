# Individual Module Data Fetching Approach

**Pattern:** Per-domain ACC REST APIs → Python flatteners → Zerobus → Bronze Delta → Silver → Delta Sharing
**Out of scope for Phase 1:** Webhooks, Data Connector bulk export, real-time push
**Cadence:** Polled (configurable per entity, default 15 min)

---

## 1. Service Inventory (All 25 Domains)

### Tier 1 — Mature REST APIs (Phase 1 — Priority)

| # | Domain | REST API Base Path | Primary Entity | Nested Arrays |
|---|---|---|---|---|
| 1 | Issues | `/construction/issues/v2/projects/{projectId}` | issues | comments, attachments, customAttributes, rootCauses, linkedDocuments |
| 2 | RFIs | `/construction/rfis/v2/projects/{projectId}` | rfis | responses, attachments, distributionList, customAttributes |
| 3 | Submittals | `/construction/submittals/v2/projects/{projectId}` | submittals | items, attachments, transmittals, distributionList |
| 4 | Forms | `/construction/forms/v1/projects/{projectId}` | forms | sections, fields, attachments, signatures |
| 5 | Photos | `/construction/photos/v1/projects/{projectId}` | photos | tags, locations, attachments |

### Tier 2 — Full REST, Polled (Phase 1 — Priority)

| # | Domain | REST API Base Path | Primary Entity | Nested Arrays |
|---|---|---|---|---|
| 6 | Cost — Budgets | `/construction/cost/v1/containers/{containerId}/budgets` | budgets | lineItems, contingency, transactions |
| 7 | Cost — Contracts | `/construction/cost/v1/containers/{containerId}/contracts` | contracts | lineItems, paymentTerms, attachments |
| 8 | Cost — Change Orders | `/construction/cost/v1/containers/{containerId}/change-orders` | change_orders | lineItems, approvals, attachments |
| 9 | Assets | `/construction/assets/v1/projects/{projectId}/assets` | assets | customAttributes, statusHistory, attachments |
| 10 | Locations | `/construction/locations/v2/projects/{projectId}/trees` | locations | children (recursive tree) |
| 11 | Sheets | `/construction/sheets/v1/projects/{projectId}/sheets` | sheets | versions, exports, references |
| 12 | Documents (Docs) | `/data/v1/projects/{projectId}/folders` + `/items` + `/versions` | documents | versions, relationships |
| 13 | Markups | `/construction/markups/v1/projects/{projectId}` | markups | annotations, attachments |
| 14 | Reviews | `/bim360/reviews/v1/projects/{projectId}/reviews` | reviews | reviewers, comments, attachments |

**Phase 1 total: 14 services / domains**

### Tier 3 — Limited / Less Mature (Phase 2)

| # | Domain | REST API |
|---|---|---|
| 15 | Takeoff | `/construction/takeoff/v1/projects/{projectId}` |
| 16 | Model Coordination | `/bim360/modelcoord/v1/...` |
| 17 | Meetings | `/construction/meetings/v1/projects/{projectId}` |
| 18 | Workflows | `/construction/workflows/v1/projects/{projectId}` |
| 19 | Quality / Checklists | (overlaps Forms in newer ACC) |
| 20 | Activity Logs | Account audit API |

### Tier 4 — Admin / Reference (Phase 2 — daily poll, low volume)

| # | Domain | REST API |
|---|---|---|
| 21 | Account / Companies | `/hq/v1/accounts/{accountId}/companies` |
| 22 | Project Members | `/hq/v2/accounts/{accountId}/projects/{projectId}/users` |
| 23 | Projects | `/hq/v1/accounts/{accountId}/projects` |
| 24 | Roles / Permissions | `/hq/v2/accounts/{accountId}/projects/{projectId}/roles` |
| 25 | Hubs (already used) | `/project/v1/hubs` |

---

## 2. Phase 1 Bronze Tables (Approximate)

Each Tier 1/2 entity yields multiple normalized Bronze tables (parent + nested arrays):

| Service | Bronze Tables Produced |
|---|---|
| Issues | `acc_issues`, `acc_issue_comments`, `acc_issue_attachments`, `acc_issue_custom_attributes`, `acc_issue_root_causes` |
| RFIs | `acc_rfis`, `acc_rfi_responses`, `acc_rfi_attachments`, `acc_rfi_distribution`, `acc_rfi_custom_attributes` |
| Submittals | `acc_submittals`, `acc_submittal_items`, `acc_submittal_attachments`, `acc_submittal_transmittals` |
| Forms | `acc_forms`, `acc_form_sections`, `acc_form_fields`, `acc_form_attachments`, `acc_form_signatures` |
| Photos | `acc_photos`, `acc_photo_tags`, `acc_photo_locations` |
| Cost Budgets | `acc_cost_budgets`, `acc_cost_budget_line_items`, `acc_cost_budget_transactions` |
| Cost Contracts | `acc_cost_contracts`, `acc_cost_contract_line_items`, `acc_cost_contract_payment_terms` |
| Cost Change Orders | `acc_cost_change_orders`, `acc_cost_co_line_items`, `acc_cost_co_approvals` |
| Assets | `acc_assets`, `acc_asset_custom_attributes`, `acc_asset_status_history` |
| Locations | `acc_locations` (recursive tree flattened to parent_id chain) |
| Sheets | `acc_sheets`, `acc_sheet_versions`, `acc_sheet_exports` |
| Documents | `acc_documents`, `acc_document_versions`, `acc_document_relationships` |
| Markups | `acc_markups`, `acc_markup_annotations`, `acc_markup_attachments` |
| Reviews | `acc_reviews`, `acc_review_reviewers`, `acc_review_comments` |

**Estimated Phase 1 Bronze table count: ~50 tables**

---

## 3. Required APS OAuth Scopes (Phase 1)

```
data:read
data:write          (Zerobus auth on Databricks side; not ACC)
account:read
bucket:read
viewables:read
```

Per-entity scopes required for Tier 1/2 reads are all covered by `data:read` + `account:read`. Cost API additionally requires the user to have Cost permissions on the project.

---

## 4. Cross-Cutting Concerns (Implementation Conventions)

### 4.1 Authentication

- **3-legged user token** for all entity-level reads (Issues, RFIs, Cost, etc.).
- **2-legged app token** for account-level reads (Companies, Projects list).
- Token refresh handled centrally in `acc_client.get_valid_token(user_id)`.
- Refresh threshold: 5 min before expiry.

### 4.2 Pagination

ACC APIs use two patterns:

| Pattern | Used by | Mechanism |
|---|---|---|
| `limit` + `offset` | Most legacy APIs (RFIs v2, Submittals, Cost) | `?limit=200&offset=400` |
| Cursor-based | Newer APIs (Issues v2, Forms v1, Assets) | `?limit=200`; response includes `pagination.nextUrl` |

**Generic helper:** `acc_client.paginate(url, params)` yields entities one at a time, handling either style transparently.

```python
def paginate(url, params, token):
    while url:
        resp = http_get(url, params, token)
        for item in resp.get("results", []) or resp.get("data", []):
            yield item
        url = resp.get("pagination", {}).get("nextUrl")
        params = None  # nextUrl already includes query params
```

**Default page size:** 200 (max allowed by most ACC endpoints).

### 4.3 Rate Limiting

- ACC enforces ~300–500 req/min per app globally; some endpoints (Cost) have stricter limits.
- Handle `429 Too Many Requests` with **exponential backoff + jitter**.
- Default retry: up to 5 attempts, base delay 1s, max delay 30s.
- Respect `Retry-After` response header when present.
- **Bounded concurrency**: max 5 concurrent requests per service (configurable).

```python
class RateLimitedSession:
    def __init__(self, max_concurrent=5, max_retries=5):
        self.semaphore = threading.Semaphore(max_concurrent)
        ...
```

### 4.4 Retry / Resilience

| HTTP status | Action |
|---|---|
| 200–299 | Success |
| 401 | Refresh token, retry once |
| 403 | Permission issue — log, skip entity, do NOT retry |
| 404 | Entity deleted upstream — log, skip |
| 429 | Backoff + retry per `Retry-After` |
| 5xx | Exponential backoff, up to 5 retries |
| Network error | Retry up to 3 times with 2s delay |

### 4.5 Watermarks (Per Entity)

Replace single `data_connector` watermark with one per entity:

```
issues_watermark
rfis_watermark
cost_budgets_watermark
cost_contracts_watermark
cost_change_orders_watermark
assets_watermark
... etc per service
```

- Stored in `state_store` keyed by `(user_id, project_id, entity_name)`.
- Watermark = ISO-8601 timestamp of the most recent `updatedAt` successfully ingested.
- Updated **only after** all rows for that batch are flushed to Zerobus.
- On first run (no watermark), do full backfill (see 4.7).

### 4.6 Delete Detection

ACC REST APIs handle deletes inconsistently:

| API | Delete behavior | Strategy |
|---|---|---|
| Issues v2 | Soft-delete: returns `status="deleted"` | Capture `status` column; Silver filters it |
| RFIs v2 | Hard-delete: row disappears | Periodic full re-list (weekly); Silver MERGE flags missing IDs |
| Cost | Soft-delete via `deletedAt` timestamp | Capture `deletedAt`; Silver filters non-null |
| Assets | Soft-delete via `isDeleted` flag | Capture `is_deleted`; Silver filters |
| Photos | Hard-delete | Periodic full re-list |

**Recommendation:** Run a weekly full enumeration per entity to detect hard-deletes, in addition to incremental polling.

### 4.7 Backfill (First Run)

- New project → no watermark → full backfill required.
- Backfill chunks by date range to avoid timeout: walk monthly from project creation date to today.
- Use a separate command/code path: `backfill_entity(project_id, entity, since=None, until=None)`.
- Mark backfill in progress in `state_store` so incremental polls don't run concurrently for that entity.

### 4.8 Schema Drift

- ACC adds fields to API responses over time.
- Flatteners use `.get(field, default)` — unknown fields are ignored (forward-compatible).
- Bronze table DDL is **explicit** (not inferred) — adding new columns requires `ALTER TABLE ADD COLUMN`.
- A schema-drift detector logs unknown fields seen in the wild for later promotion to Bronze schema.

### 4.9 Idempotency / Deduplication

- Zerobus is at-least-once → Bronze can have duplicates.
- Bronze is **append-only**, no dedup at write time.
- Silver MERGE on primary key + take latest by `updated_at` / `ingested_at`:

```sql
MERGE INTO silver.acc_issues AS tgt
USING (
    SELECT * FROM (
        SELECT *,
               ROW_NUMBER() OVER (PARTITION BY issue_id ORDER BY ingested_at DESC) AS rn
        FROM bronze.acc_issues
        WHERE ingested_at > (SELECT COALESCE(MAX(silver_ingested_at), '1970-01-01') FROM silver.acc_issues)
    ) WHERE rn = 1
) AS src
ON tgt.issue_id = src.issue_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *;
```

### 4.10 Empty Arrays / Nulls

- ACC sometimes returns `null` instead of `[]` for empty arrays.
- Always coalesce: `for item in (parent.get("comments", []) or []):`
- Skip `.append()` calls when child arrays are empty — no empty rows in Bronze.

### 4.11 Common Bronze Columns (Schema Convention)

Every Bronze table includes:

| Column | Type | Source |
|---|---|---|
| `<entity>_id` (primary key) | STRING NOT NULL | ACC `id` |
| `project_id` | STRING NOT NULL | URL path / context |
| `account_id` | STRING NOT NULL | URL path / context |
| `created_at` | STRING | ACC `createdAt` (kept as STRING in Bronze, cast in Silver) |
| `updated_at` | STRING | ACC `updatedAt` |
| `ingested_at` | STRING | UTC ISO timestamp at write time |
| `source_system` | STRING | Constant: `'acc_rest_v1'` |
| `raw_payload` | STRING | Optional: full JSON for debugging (toggleable) |

For child tables, also include the parent FK (`issue_id`, `rfi_id`, `cost_budget_id`, etc.).

### 4.12 Naming Conventions

- **Schema names:** `bronze`, `silver`, `gold`
- **Table names:** `acc_<entity>` (e.g. `acc_issues`, `acc_rfi_responses`)
- **Column names:** `snake_case` (convert ACC `camelCase` in flattener)
- **Primary keys:** `<entity>_id` (e.g. `issue_id`, never just `id`)
- **Foreign keys:** `<parent_entity>_id` (e.g. `issue_id` in `acc_issue_comments`)

### 4.13 Logging / Observability

- Per-entity ingest logs: `entity, project_id, rows_fetched, rows_pushed, duration_ms, errors`
- Per-batch log row written to `connector_runs` state table.
- Failed entity does not fail the whole sync — log and continue with next entity.
- Dashboard surfaces per-entity success rate and last-success timestamp.

---

## 5. Code Layout (Suggested)

```
acc-connector/
├── backend/
│   ├── acc_client.py              # OAuth, low-level HTTP, pagination, retry
│   ├── zerobus_writer.py          # Wrapper around Zerobus SDK (open/append/flush)
│   ├── bootstrap.py               # Catalog/schema/Bronze DDL setup
│   ├── sync_service.py            # Orchestrator: loops services, per-entity ingest
│   ├── state_store.py             # Watermarks, run history (existing — keep)
│   ├── flatteners/
│   │   ├── __init__.py
│   │   ├── issues.py              # flatten_issue(json) -> dict[table, rows]
│   │   ├── rfis.py
│   │   ├── submittals.py
│   │   ├── forms.py
│   │   ├── photos.py
│   │   ├── cost_budgets.py
│   │   ├── cost_contracts.py
│   │   ├── cost_change_orders.py
│   │   ├── assets.py
│   │   ├── locations.py
│   │   ├── sheets.py
│   │   ├── documents.py
│   │   ├── markups.py
│   │   └── reviews.py
│   └── ingestors/
│       ├── __init__.py
│       ├── base.py                # BaseIngestor: fetch loop + Zerobus writes
│       ├── issues.py              # IssuesIngestor uses flatten_issue
│       ├── rfis.py
│       └── ... (one per entity)
└── notebooks/
    └── silver_transform.py         # MERGE-based Silver layer (keep, update for new schema)
```

---

## 6. Phase 1 Build Sequence

| Week | Deliverable |
|---|---|
| 1 | `zerobus_writer.py` + `acc_client.paginate()` + `acc_client.RateLimitedSession` + Bronze DDL automation in `bootstrap.py` |
| 2 | Issues end-to-end: fetch → flatten → Zerobus → Bronze → Silver MERGE. Validate vs ACC UI. |
| 3 | RFIs + Submittals + Forms (clone Issues pattern). |
| 4 | Photos + Assets + Locations. |
| 5 | Cost (Budgets, Contracts, Change Orders) — biggest surface, allow extra time. |
| 6 | Sheets + Documents + Markups + Reviews. |
| 7 | Silver layer hardening, dedup, schema drift detector, weekly full re-enumeration job. |
| 8 | Delta Sharing setup (one-time SQL + UI panel) + production hardening. |

**Total estimated effort: 8 weeks for Phase 1 (14 services).**

---

## 7. Out of Scope for Phase 1

- Webhooks (deferred to Phase 2 / 3)
- Tier 3 services (Takeoff, Model Coordination, Meetings, Workflows, Quality, Activity Logs)
- Tier 4 admin/reference services (Companies, Members, Projects, Roles)
- Real-time push (polling only, default 15 min cadence)
- Removing existing Data Connector code (keep alongside as fallback during transition)

---

## 8. Acceptance Criteria

Phase 1 is complete when:

- [ ] All 14 Tier 1/2 services have ingestors writing to Zerobus → Bronze Delta
- [ ] Silver layer dedup MERGE runs successfully for each entity
- [ ] Pagination, rate limiting, retry, and 401-refresh are centralized and tested
- [ ] Per-entity watermarks persist correctly across runs
- [ ] Backfill command can re-load any entity from a chosen date
- [ ] Weekly full re-enumeration job catches hard-deletes
- [ ] Delta Sharing exposes Silver tables to one test recipient
- [ ] Dashboard shows per-entity status, last-success time, and row counts
- [ ] Existing Data Connector code is preserved (feature flag) for rollback

---

## 9. Open Questions To Resolve Before Build

1. Confirm Zerobus GA status and region availability for the customer's Databricks workspace.
2. Confirm APS app has all required scopes (`data:read`, `account:read`).
3. Confirm Cost API permissions on the test project (Cost requires explicit user role).
4. Decide poll cadence per entity (default 15 min; Cost may need slower).
5. Decide whether to keep `raw_payload` column in Bronze (storage cost vs debuggability).
6. Decide Silver MERGE cadence (per ingest vs scheduled hourly).
