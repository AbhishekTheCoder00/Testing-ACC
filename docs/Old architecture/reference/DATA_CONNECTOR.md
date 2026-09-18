# Data Connector API — Foundational Knowledge

> **Purpose:** Reference document capturing what the ACC Data Connector API is, how it
> exposes data, and how that shapes the production architecture for the Lakehouse pipeline.
>
> **Audience:** Engineers and architects designing the production sync (post-POC).
>
> **Status:** Conceptual reference. Domain counts and per-table delete-pattern classifications
> are partially verified from public schema docs and partially inferred — see Section 8.

---

## TL;DR — Five-line summary

1. Data Connector is **job-based bulk export**, not a fetch-records API. You submit a job, poll, then download CSVs.
2. ACC data is exposed as a **flat warehouse-style schema of ~120–150 tables**, organized into ~12 domains.
3. Nested data (e.g. an issue's attachments) is **normalized into separate tables linked by foreign keys**, not nested JSON.
4. Every table is one of two types: **snapshot** (current state, full pull each time) or **activity** (event log, date-range pull, 31-day max lookback).
5. Most entity tables have a **`deleted_at` column (soft delete)**. A small set of reference / lookup tables don't — those need special handling.

---

## 1. What the Data Connector API Is

### Job-submission model

Unlike per-module REST APIs (e.g. `/construction/issues/v1/projects/{id}/issues`), the Data Connector API does not return records inline. The interaction is asynchronous:

1. **Submit** a job: *"Prepare an extract for project X, date range D1–D2, these tables."*
2. **Poll** until the job state is `success`. Could take seconds to many minutes.
3. **Download** the resulting CSV files — one CSV per table requested.

### Why this matters for our architecture

| Property | Per-module REST | Data Connector |
|---|---|---|
| Output format | Nested JSON | Flat CSV |
| Coverage | One module per call | Many tables in one job |
| Latency | Real-time | Minutes (server-side processing) |
| Best for | Live UI, low-latency reads | Lakehouse / warehouse / BI ingestion |
| Filtering | Per-record query params | Per-job date range (activities only) |
| Hard delete handling | Manual reconciliation | Built in via tombstones + activity feed |

The Data Connector trades latency for **scale, completeness, and a normalized schema** — exactly what a Lakehouse needs.

---

## 2. How Data Comes Out — CSV with Flattened Schema

### One CSV per table, all flat

The Data Connector returns each table as a separate CSV. Every column is a scalar — string, number, date, boolean. There are no nested JSON columns. No arrays in cells.

### Nested data becomes separate tables, linked by foreign keys

When a source entity has a sub-collection in the REST API's nested JSON (e.g. an issue has many attachments), Data Connector splits it across multiple tables:

```
REST nested JSON                        Data Connector CSVs
────────────────────                    ─────────────────────
{                                       issues.csv
  "id": "iss-1234",                       issue_id  | title  | status | ...
  "title": "Crack in column",             iss-1234  | Crack..| Open   | ...
  "attachments": [
    { "id": "att-9", ... },             attachments.csv
    { "id": "att-10", ... }               attachment_id | issue_id | url | ...
  ],                                      att-9         | iss-1234 | ... |
  "comments": [...]                       att-10        | iss-1234 | ... |
}                                       comments.csv
                                          comment_id | issue_id | body | ...
                                          ...        | iss-1234 | ...  |
```

Every child table carries a foreign-key column (here, `issue_id`) pointing back to the parent. This means:

- The schema is **already normalized** — no JSON parsing required in Bronze.
- Relationships are queryable via standard SQL JOINs in Silver / Gold.
- Each table has a **stable primary key** that we can use as the MERGE key.

### Implication for our pipeline

- **No `from_json` / nested-struct handling needed.** Bronze just reads CSVs.
- **MERGE is straightforward** — every snapshot table has a clear primary key from its registry entry.
- **Joins move from "parse JSON" to "JOIN tables"** — exactly the Lakehouse pattern we want.

---

## 3. The ~12 Domains

The Data Connector groups tables into roughly 12 domains, mirroring ACC's product modules. Each domain contains 5–20 tables.

| # | Domain | What it covers | Example tables |
|---|---|---|---|
| 1 | **admin** | Accounts, projects, users, companies, roles, project membership | `accounts`, `projects`, `users`, `companies`, `project_users` |
| 2 | **issues** | Field issues / observations, types, root causes, attachments | `issues`, `issue_types`, `issue_subtypes`, `root_causes`, `attachments`, `placements`, `viewables` |
| 3 | **rfis** | Requests for Information, comments, attachments | `rfis`, `rfi_attachments`, `rfi_comments` |
| 4 | **cost** | Budgets, contracts, change orders, expenses, transactions | `cost_budgets`, `cost_contracts`, `cost_change_orders`, `cost_expenses` |
| 5 | **submittals** | Submittal items, specs, transmittals, revisions | `submittals`, `submittal_specs`, `submittal_transmittals` |
| 6 | **docs** | Document management — folders, items, versions, permissions | `docs_folders`, `docs_items`, `docs_versions`, `docs_permissions` |
| 7 | **assets** | Asset register, categories, statuses, custom attributes | `assets`, `asset_categories`, `asset_statuses`, `asset_custom_attributes` |
| 8 | **forms** | Form templates, instances, sections, items, responses | `forms_templates`, `forms_instances`, `forms_sections`, `forms_items` |
| 9 | **checklists** | (May overlap with forms — checklists are a forms subtype) | `checklist_templates`, `checklist_instances` |
| 10 | **sheets** | Sheets, version sets, markups | `sheets`, `sheet_versions`, `sheet_markups` |
| 11 | **bridge** | Cross-project sharing — shared entities and links | `bridge_links`, `bridge_shared_items` |
| 12 | **photos / media** | Project photos, albums, EXIF metadata | `photos`, `photo_albums` |

> **Note:** the exact list of 12 domains and their boundaries should be validated against the
> live `data-connector/v1/doc/schema?name=<domain>` endpoint before locking the registry.
> Some sources merge `forms` and `checklists` into one; `photos` is sometimes a sub-domain of `docs`.

Total table count across all domains: **~120–150** depending on which optional modules a customer has enabled.

---

## 4. Two Bucket Types — Snapshot vs Activity

This is the most important conceptual distinction in the Data Connector model. **Every table belongs to exactly one of these two buckets, and they behave fundamentally differently.**

### Bucket 1 — Snapshot tables (the "current state" tables)

- One row per entity, identified by a primary key (e.g. `issue_id`, `budget_id`).
- The API returns the **entire table** every time — no date filter.
- Captures **current state** as of the moment the job ran.
- Deleted rows usually stay in the table with `deleted_at` populated (soft delete).
- Examples: `issues`, `cost_budgets`, `rfis`, `assets`, `submittals`, `docs_items`.

### Bucket 2 — Activity tables (the "event log" tables)

- One row per event — "issue 1234 was created", "issue 1234 was assigned", "issue 1234 was deleted".
- The API returns **only events in the requested date range** (`startDate` / `endDate`).
- Captures **history of changes**, including intermediate states.
- **31-day retention limit** — events older than 31 days are gone forever.
- 9 activity feeds in total: `issues_activities`, `cost_activities`, `rfis_activities`, `submittals_activities`, `docs_activities`, `assets_activities`, `sheets_activities`, `admin_activities`, `bridge_activities`.
- 3 supplementary "changes" feeds (field-level diffs): `issues_changes`, `rfis_changes`, `cost_changes`.

### Why both exist

A snapshot is a single photograph at one moment in time. It cannot show:

- **Intermediate states** — if an issue went `Open → In Review → Closed → Reopened → Closed` between two daily syncs, the snapshot only sees the final `Closed`.
- **Audit trail** — who did what when, who reassigned it, who tried to delete it.
- **Event throughput** — count of issues created per day, average time-to-close, etc.

The activity feed answers all of these. Together, the two buckets cover **both** "what is the current state" **and** "what has happened over time."

### How "incremental" works in each bucket

| | Snapshot | Activity |
|---|---|---|
| Watermark used as API filter? | No | Yes (it's the `startDate`) |
| Watermark's role | Schedule clock — "is it time to run?" | Real query parameter |
| Volume per sync | Always full table | Just the new window |
| Silver pattern | MERGE on primary key | APPEND + dedupe on event ID |
| Hard-delete handling | `deleted_at` column on the row | `deleted` activity verb |
| Failure recovery | Re-run today's job | Re-run today's window — but watch the 31-day cap |

---

## 5. Mental Visual Diagram

```
              ┌─────────────────────────────┐
              │   ACC Data Connector API    │
              │   (one job, many tables)    │
              └──────────────┬──────────────┘
                             │
                ┌────────────┴────────────┐
                │                         │
                ▼                         ▼
      ┌──────────────────┐      ┌──────────────────┐
      │  SNAPSHOT CSV    │      │  ACTIVITY CSV    │
      │  full table,     │      │  events in       │
      │  every row       │      │  date range      │
      │                  │      │                  │
      │  e.g.            │      │  e.g.            │
      │   issues         │      │   issues_        │
      │   cost_budgets   │      │     activities   │
      │   assets         │      │   cost_          │
      │   ...            │      │     activities   │
      └────────┬─────────┘      └────────┬─────────┘
               │                         │
               ▼                         ▼
      ┌──────────────────┐      ┌──────────────────┐
      │   UC Volume      │      │   UC Volume      │
      │   (raw CSV)      │      │   (raw CSV)      │
      └────────┬─────────┘      └────────┬─────────┘
               │                         │
               ▼                         ▼
      ┌──────────────────┐      ┌──────────────────┐
      │   bronze.*       │      │   bronze.*_      │
      │   (append)       │      │     activities   │
      └────────┬─────────┘      └────────┬─────────┘
               │                         │
               │  MERGE on PK            │  APPEND + dedupe
               ▼                         ▼
      ┌──────────────────┐      ┌──────────────────┐
      │   silver.*       │      │   silver.*_      │
      │   current state  │      │     activities   │
      │   (deletes via   │      │   event timeline │
      │    deleted_at)   │      │                  │
      └──────────────────┘      └──────────────────┘
            │                         │
            ▼                         ▼
       BI / GenAI / ML        Audit / dashboards /
       (current state)         change-data feeds
```

**Three things to take from this diagram:**

1. The fork at the top is the most important concept — every table is either a snapshot or an activity, and they flow through different Silver patterns.
2. Snapshots use **MERGE** (overwrite by primary key); activities use **APPEND + dedupe** (events are immutable).
3. The two end-states serve different downstream uses — current-state queries vs event-history queries.

---

## 6. Delete-Pattern Map by Domain

ACC tables fall into 5 delete-signal patterns. The pattern determines how delete handling
works in incremental sync, and whether the simple "skip empty CSV" rule is safe.

| Pattern | Signal | Skip-empty-CSV safe? | Examples |
|---|---|---|---|
| **A — `deleted_at` timestamp** | Nullable timestamp + `deleted_by` columns | **Yes** — empty truly means "no data" | Most entity tables |
| **B — `is_active` boolean** | Status flag column | **Yes** | Reference tables (types, statuses) |
| **C — Status enum sentinel** | `status='void'`, `status_id=4 Void` | **Yes** | RFIs, some submittal tables |
| **D — Activity-feed-only** | Verb in `*_activities` row | N/A — applies to events | Cross-validation use case |
| **E — No delete signal** | None — row just disappears | **No** — empty is ambiguous, needs alert | Lookup / join / reference tables |

### Domain-by-domain classification (best-effort, validate before locking)

| Domain | Primary delete pattern | Notes |
|---|---|---|
| **issues** | A | `deleted_at`, `deleted_by` confirmed on `issues`, `attachments`, `placements`, `viewables`. `issue_types`, `issue_subtypes`, `root_causes`, `root_cause_categories` use Pattern B (`is_active`). |
| **rfis** | C | RFIs use a status enum — `status='void'` indicates a deleted RFI in v3. |
| **cost** | A | `deleted_at` on most entity tables (budgets, contracts, change orders). 14 cost entities, all with delete webhooks. |
| **assets** | A | UI shows "Deleted assets" tab — confirms tombstoning at source. `deleted_at` expected on `assets` table. |
| **submittals** | A or C | Submittals have status workflow; Pattern A on the main entity is likely but unconfirmed. |
| **docs** | A | `docs_items`, `docs_versions` likely use Pattern A — soft delete is standard for document management. |
| **forms / checklists** | A | Templates and instances likely use Pattern A. |
| **sheets** | A | Sheets and version sets likely use Pattern A. |
| **admin** | Mixed (A and B) | `users` and `companies` use Pattern B (`is_active`); `projects` use Pattern A. |
| **bridge** | A | Shared links and items expected to soft-delete. |
| **photos / media** | A | Media items expected to soft-delete (consistent with docs). |
| **Reference / lookup tables across all domains** | **E** | Examples: LBS locations (location-breakdown structure), naming standards, custom attribute mappings, cost code mappings. These are often **no-delete-signal** and need special empty-CSV handling. |

### Implication for the registry

Every table's registry entry must include a `delete_pattern` field. The empty-CSV behavior
and the Silver MERGE behavior both depend on it:

```yaml
- table: issues
  domain: issues
  bucket: snapshot
  primary_key: issue_id
  delete_pattern: A
  delete_columns: [deleted_at, deleted_by]
  empty_csv_policy: skip_safe
  active_filter: "deleted_at IS NULL"

- table: issue_types
  domain: issues
  bucket: snapshot
  primary_key: issue_type_id
  delete_pattern: B
  delete_columns: [is_active]
  empty_csv_policy: skip_safe
  active_filter: "is_active = true"

- table: lbs_locations
  domain: admin
  bucket: snapshot
  primary_key: location_id
  delete_pattern: E
  delete_columns: []
  empty_csv_policy: alert_no_advance   # empty is ambiguous — human review required
  active_filter: null

- table: issues_activities
  domain: issues
  bucket: activity
  primary_key: activity_id
  delete_pattern: D
  empty_csv_policy: skip_safe          # empty = no events in window, always safe
  partition_column: created_at
```

---

## 7. Why Activity Tables Matter for Insights (Not Sync)

For pure "keep Silver in sync with current state," activity tables add nothing — the snapshot
plus its `deleted_at` column is sufficient. Activity tables exist to support **insight use cases
that snapshots fundamentally cannot serve**:

### 7.1 Intermediate states between syncs
If issue 1234 went `Open → In Review → Closed → Reopened → Closed` in one day, the daily
snapshot only shows the final `Closed`. Activity feed captures all 5 transitions.

### 7.2 Audit trail / who-did-what / when
Compliance reports like "who modified this cost budget in Q2" or "build a change log for this RFI"
require event-level data. Snapshot alone cannot answer these.

### 7.3 Change-data-feed-style downstream consumption
- ML feature: "average time from issue creation to closure" — needs `created` and `closed` events.
- Alert: "RFI marked overdue more than once" — needs the event log.
- BI dashboard: "throughput per day per discipline" — needs activity counts per day.

### 7.4 Hard-delete detection for Pattern E tables
For the small set of reference tables that don't have `deleted_at`, the activity feed is the only
trustworthy source for "this row was deleted at time T."

### 7.5 Cross-validation / data quality
"Does every `deleted` activity have a matching `deleted_at`-populated row in the snapshot?"
Mismatches reveal source-system bugs.

---

## 8. What's Verified vs Inferred

To be transparent about the evidence behind the claims in this document:

### Verified (read directly from public Autodesk docs in research)
- Job-based async model with submit / poll / download lifecycle
- CSV output format with flat (non-nested) columns
- 9 activity feeds and their names
- 3 changes feeds (Issues / RFIs / Cost only)
- 31-day retention on activity / changes feeds
- `issues` schema columns including `deleted_at` (col 31), `deleted_by` (col 35)
- Pattern B on `issue_types`, `root_causes`, `issue_subtypes` (`is_active` columns)
- ACC Build UI shows "Deleted issues" and "Deleted assets" tabs (empirical)

### Inferred from patterns (high confidence, but not directly verified)
- ~120–150 tables total
- Pattern A on `cost`, `submittals`, `docs`, `forms`, `sheets`, `assets` entity tables
- The 12-domain split (some sources merge or split differently)
- Per-domain delete pattern map in Section 6

### Open questions to resolve before locking the registry
- Confirm exact domain count and naming via `data-connector/v1/doc/schema?name=<domain>` for all 12.
- Confirm Pattern A on the main entity table for: `cost_budgets`, `submittals`, `docs_items`,
  `assets`, `forms_instances`, `sheets`.
- Identify all Pattern E (no-delete-signal) tables — most likely lookup / reference / join tables.
- Confirm the activity-feed list is exactly 9 and the changes-feed list is exactly 3.

---

## 9. References

- Autodesk APS Data Connector reference: `https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector/`
- Schema endpoint: `https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector/v1/doc/schema?name=<domain>`
- ACC Build UI delete tabs: confirmed empirically via in-product screenshots
- Related repo docs: [PLATFORM.md](../architecture/PLATFORM.md), [SESSION_STATE.md](../meta/SESSION_STATE.md)
