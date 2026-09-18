# ACC API Research — for Ingestion Decision

> Generated: 2026-05-16  
> Scope: per-service REST/DC capability reference; Bronze ingestion lock is [PLATFORM.md](../architecture/PLATFORM.md).  
> Authority: Only `aps.autodesk.com`, `forge.autodesk.com`, and `docs.databricks.com` citations below qualify as authoritative per research rules; APS blog posts on `aps.autodesk.com/blog` are cited where used. Community pages are explicitly labeled.

---

## 1. Per-service capability matrix

**Convention:** “Primary list endpoint” means the documented operation used to enumerate project-scoped entities (or hub/account scope for admin). Where the HTTP reference page was not retrieved in extractable form, cells state **Not found in official docs** rather than inferred.

**API surface inventory (`api_product`):** The Forma (**ACC**) REST bundle is enumerated under **Developer's Guide → API Reference** in the APS Forma API documentation ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)). That sidebar lists modular APIs (Issues, RFIs, Submittals, Cost, Locations, Photos, Assets, Sheets, Forms, Reviews, Hub Admin, Data Connector, ACC Files tooling, Takeoff, Transmittals, AutoSpecs, Model Coordination, Model Properties, Relationships, Weather, etc.). Exact “14 vs 25” counts are **Not found in official docs** as an explicit integer; treat the overview’s API Reference tree as authoritative.

### Cost Management — webhook-exposed entities (distinct event stems)

Webhook event names documented under Cost Management are: `budget`, `budgetPayment`, `contract`, `cor`, `costPayment`, `expense`, `expenseItem`, `mainContract`, `mainContractItem`, `oco`, `pco`, `rfq`, `scheduleOfValue`, `sco`, `segmentValue`, plus `project.initialized-1.0` ([cost events hub](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/cost_management_events)).  
The REST Cost API reference exposes additional resource families (examples: Actions, Attachments, Attribute Definitions/Values, Budget Code templates/segments/values, Budgets-contracts linking, Documents, Payments/Payment Items, Sub Cost Items, Tax, Timesheet, Performance Tracking, Workflow)—see sidebar under **Cost Management** in ([Forma APIs overview — API Reference](https://aps.autodesk.com/en/docs/acc/v1/overview/)).

### 1.1 Consolidated Service Group Capability & Sync Frequency

| # | Service Group | DC Standard | DC CDC (Beta) | `updatedAt` Filter (Query) | `updatedAt` Field (Response) | Delete Filter (Query) | Delete Field (Response) | Webhook Events | Pattern | Sync Behaviour | Cost per Cycle | Best Sync Frequency | Reasoning |
|---|---|:-:|:-:|---|---|---|---|---|---|---|---|---|---|
| 1 | **issues** | YES | YES | `filter[updatedAt]` confirmed | `updatedAt` in response | `filter[deleted]=true/false` | `deletedAt` (via webhook payload) | `issue.created/updated/deleted/restored/unlinked-1.0` | Q+R + Webhook | Push-based; webhook triggers refetch of changed record only | Lowest — webhook push, no polling | Real-time | Full CRUD webhook + query filter + response fields |
| 2 | **cost** | YES | YES | `filter[updatedAt]` confirmed | Not verified | No | No REST delete field; webhook `*.deleted-1.0` only | `budget/contract/changeorder/costitem .created/updated/deleted-1.0` (15 sub-entities) | Q + Webhook | Push-based; webhook triggers refetch; deletes via webhook signal only | Lowest — webhook push, no polling | Real-time | 15 sub-entity webhooks cover full lifecycle |
| 3 | **reviews** | YES | NO | `filter[updatedAt]` confirmed | Not verified | N/A (non-deletable) | N/A (non-deletable) | `review.created-1.0`, `review.closed-1.0` | Q + Webhook | Push on create/close; poll `filter[updatedAt]` for edits between events | Low — webhook + light polling gap-fill | Near real-time (~10 min) | Reviews are immutable audit records; webhook catches creates/closes; poll for edits |
| 4 | **submittalsacc** | YES | YES | `filter[updatedAt]` confirmed | Not verified | No | No | No | Q only | Poll with `filter[updatedAt]`; fetch delta only; no delete signal from REST | Low — delta fetch, proportional to changes | Hourly | No webhook; CDC beta hourly covers deletes; update+delete must match cadence |
| 5 | **rfis** | YES | YES | `POST /search:rfis` body filter (likely `updatedAt`) | Not verified | No | `voidedAt` field on voided RFIs | No | Q + R(delete) | Poll via POST body filter; detect soft-deletes via `voidedAt` in response | Low-Medium — delta fetch + client-side void check | Near real-time (~10 min) | POST body filter for updates + response-side soft-delete |
| 6 | **admin** | YES | YES | `filter[updatedAt]` confirmed (project listing) | Not verified | No | No | No | Q only | Poll with `filter[updatedAt]`; no delete signal | Low — delta fetch, small dataset | Hourly | Admin data changes infrequently; CDC beta hourly covers deletes |
| 7 | **locations** | YES | YES | No `filter[updatedAt]` on GET nodes | Not verified | No | No | No | None | Full pull every cycle; compare client-side against previous state | High — full dataset every time | Hourly | No REST incremental path; CDC beta is only delta source |
| 8 | **schedule** | YES | YES | No confirmed REST API with date filter | Not verified | No | No | No | None | Full pull every cycle | High — full dataset every time | Hourly | No REST incremental path; CDC beta only |
| 9 | **sheets** | YES | YES | Unconfirmed | Not verified | No | No | No | None (unconfirmed) | Full pull assumed until filter confirmed | High — assumed full pull | Hourly | CDC beta available; REST filter unconfirmed |
| 10 | **meetingminutes** | YES | YES | No confirmed REST API with date filter | Not verified | No | No | No | None | Full pull every cycle | High — full dataset every time | Hourly | CDC beta is the only incremental path |
| 11 | **transmittals** | YES | YES | Unconfirmed | Not verified | No | No | No | None (unconfirmed) | Full pull assumed until filter confirmed | High — assumed full pull | Hourly | CDC beta available; REST filter unconfirmed |
| 12 | **assets** | YES | NO | `filter[updatedAt]` confirmed | `updatedAt` confirmed | `includeDeleted=true` (gate param) | `deletedAt`, `deletedBy`, `isActive` confirmed | No | Q+R (full) | Poll with `filter[updatedAt]` + `includeDeleted=true`; single call returns updated + deleted records | Lowest — single delta call covers updates + deletes | Near real-time (~10 min) | Best non-webhook service; complete Q+R for both dimensions |
| 13 | **submittals** | YES | NO | `filter[updatedAt]` confirmed | Not verified | No | No | No | Q only | Poll with `filter[updatedAt]`; delta fetch; no delete signal | Low — delta fetch, but blind to deletes | Manual snapshot | Query filter exists but no delete detection; snapshot diff needed for completeness |
| 14 | **photos** | YES | NO | `POST photos:filter` with date fields | Not verified | No | `deletedAt` field in response | No | Q + R(delete) | Poll via POST body filter; detect deletes via `deletedAt` in response | Low-Medium — delta fetch + client-side delete scan | Near real-time (~10 min) | POST filter for changes + response-side `deletedAt` |
| 15 | **forms** | YES | NO | Unconfirmed | Not verified | No | No | No | None (unconfirmed) | Full pull assumed | High — assumed full pull | Manual snapshot | No CDC, no webhook, REST filter unconfirmed |
| 16 | **checklists** | YES | NO | No `filter[updatedAt]` | Not verified | No | No | No | None | Full pull every cycle; no change/delete signal | Highest — full pull, blind | Manual snapshot | No incremental capability at all |
| 17 | **dailylogs** | YES | NO | No REST API for listing with date filter | Not verified | No | No | No | None | Full pull every cycle | Highest — full pull, blind | Manual snapshot | No REST list API with date filter |
| 18 | **markups** | YES | NO | No confirmed REST API with date filter | Not verified | No | No | No | None | Full pull every cycle | Highest — full pull, blind | Manual snapshot | No confirmed REST API |
| 19 | **relationships** | YES | NO | No confirmed date filter | Not verified | No | No | No | None | Full pull every cycle | Highest — full pull, blind | Manual snapshot | DC snapshot only |
| 20 | **iq** | YES | NO | No public REST API | No public REST API | No public REST API | No public REST API | No | DC only | No REST path; DC snapshot is sole extraction | N/A — DC snapshot cost only | Manual snapshot | No public REST API at all |
| 21 | **activities** | YES | NO | N/A — IS the change feed | N/A | N/A | N/A | N/A | N/A | This IS the audit log | N/A | N/A | Event log itself, not a synced entity |

**Column legend:**
- **`updatedAt` Filter (Query)** = Can you pass a date filter in the request to get only records changed after a timestamp? (server does the filtering — cheap)
- **`updatedAt` Field (Response)** = Does each record in the response body carry an `updatedAt` timestamp? (you see what changed, but server may return everything)
- **Delete Filter (Query)** = Can you request deleted/soft-deleted records via a query parameter? (e.g., `includeDeleted=true`, `filter[deleted]`)
- **Delete Field (Response)** = Does each record carry `deletedAt`/`voidedAt`/`isActive` to indicate soft-deletion?

**Pattern definitions:**
- **Q+R + Webhook** = Server-side filter + response fields + push notifications — the complete package
- **Q + Webhook** = Server-side filter + push notifications, but response-side delete field missing/unverified
- **Q+R (full)** = Server-side filter + response fields for both `updatedAt` and delete — best non-webhook tier
- **Q + R(delete)** = Server-side filter for updates + response-side delete field only (no query-side delete gate)
- **Q only** = Server-side `updatedAt` filter, no delete signal at all
- **None** = No REST incremental capability; depends entirely on DC
- **DC only** = No public REST API; DC snapshot is the sole extraction path

**Sync frequency principle:** `Best Sync Frequency = the slowest of (update detection frequency, delete detection frequency)`. Updates and deletes must be detected at the same cadence — a hybrid where creates/updates arrive faster than deletes would give downstream consumers an inconsistent picture. If delete detection is N/A (non-deletable entity), only update detection frequency applies.

**Sync frequency logic:**
- **Real-time** = Webhook events exist for both updates AND deletes → push-based notification within seconds
- **Near real-time (~10 min)** = REST `filter[updatedAt]` + delete detection confirmed (or delete N/A) → poll every ~10 min
- **Hourly** = DC CDC beta available → hourly delta with `adsk_updated_at` + `deleted_at`; OR REST `filter[updatedAt]` exists but delete detection is only available via CDC hourly
- **Manual snapshot** = No CDC, no webhook, no reliable REST date filter or no delete detection at all → DC full extract + `AUTO CDC FROM SNAPSHOT` diffing

**Bronze technique (connector):** DC Standard → **AUTO CDC FROM SNAPSHOT** (live). DC `activities` and CDC-beta deltas → **AUTO CDC** when columns confirm change-feed shape. Future REST/webhooks → **AUTO CDC** on a landed change stream (not FROM SNAPSHOT on list JSON). Per-row mapping and implementation notes: [CDC_TECHNIQUES.md](CDC_TECHNIQUES.md).

**Sources:** APS blog posts (Issues/Cost/Reviews webhook GA, Assets API beta, Photos API, RFI v3 release), APS endpoint docs (GET reviews, GET submittals/items, GET asset-statuses, GET checklists/instances), `ACC_DC_SERVICE_GROUPS.csv` for complete service list.

---

### Matrix rows (detailed per-service research)

| service_name | api_product | list_endpoint_url | updated_at_filter_supported | pagination_style | delete_semantics | activity_or_events_endpoint | rate_limit_documented | webhook_supported | notes | Sources |
|---|---|---|---|---|---|---|---|---|---|---|
| Issues | Forma Issues API v1 | `GET https://developer.api.autodesk.com/construction/issues/v1/projects/{projectId}/issues` | **YES** — `filter[updatedAt]` (+ `filter[createdAt]`, …) per query-string reference | Offset + limit (`offset`, `limit`) | Mixed: REST supports `filter[deleted]=true\|false`; webhooks expose `issue.deleted-1.0`, `issue.restored-1.0`; **Not found in official docs** whether rows always carry `deletedAt` vs hard delete semantics | APS Webhooks (`issue.*`), Data Connector `activities` **serviceGroups** mentions Issues-type activity verbs (see Activities blog) | **Not found** in retrieved Forma-specific rpm table here; Jobs API lists **429 Too Many Requests** handling ([GET jobs nested under request](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET)) | **YES** (`issue.created\|updated\|deleted\|restored\|unlinked`-1.0) system `autodesk.construction.issues` | Wildcard/group subscription supported ([blog](https://aps.autodesk.com/blog/webhook-api-acc-issue-released)); webhooks usable with 3-legged or SSA token ([blog](https://aps.autodesk.com/blog/webhook-api-acc-issue-released)). | [issues GET](https://aps.autodesk.com/en/docs/acc/v1/reference/http/issues-issues-GET), [Issues webhooks hub](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/issues_events), [Issue webhook GA blog](https://aps.autodesk.com/blog/webhook-api-acc-issue-released) |
| RFIs | Forma RFI API v3 | **Collection:** `POST https://developer.api.autodesk.com/construction/rfis/v3/projects/{projectId}/search:rfis` (no standalone GET collection; empty `{}` payload returns all) ([RFI v3 GA blog — API List](https://aps.autodesk.com/blog/autodesk-build-rfi-v3-api-released)) | **Not found** in retrieved official material for **`updatedAt`/watermark filters** inside `POST /search:rfis`; field guide + POST reference must be consulted | **Not verified** — search payload pagination not captured in cite used | Status workflow includes **`void`** in transition example (**status-transition** semantics) ([RFI v3 blog](https://aps.autodesk.com/blog/autodesk-build-rfi-v3-api-released)) | APS Webhooks: **No RFI rows** present in APS Webhooks “Supported Events” tree reviewed ([events index excerpt](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)); **Webhook NO** for ACC RFIs unless another doc contradicts — **Not found in official docs** | Same as Issues row for general ACC limits — **partial** | **NO** per reviewed webhooks taxonomy | Prefer DC + nightly diff for deletes if REST filters absent. | [RFI v3 blog](https://aps.autodesk.com/blog/autodesk-build-rfi-v3-api-released), [Webhooks supported events navigation](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET) |
| Submittals | Forma Submittals API v2 | `GET https://developer.api.autodesk.com/construction/submittals/v2/projects/{projectId}/items` | **YES** — `filter[updatedAt]` (+ many filters) | **JSON:API style links** (`pagination.nextUrl` / `previousUrl`) citing JSON:API paging help + `limit` (1–50, default 20), `offset` | **Unknown** — not extracted here | **Unknown** standalone `/activities`; DC activities include module examples in blog | Endpoint-level limits **Not verified** here | **Not found** in ACC webhooks taxonomy reviewed | Nine read endpoints GA ([GA blog](https://aps.autodesk.com/blog/autodesk-build-submittals-api-general-availability)). | [submittals items GET mirror](https://forge.autodesk.com/en/docs/acc/v1/reference/http/submittals-items-GET), [GA blog](https://aps.autodesk.com/blog/autodesk-build-submittals-api-general-availability) |
| Forms | Forms v1 (**Deprecated** in UI wording) / v2 (**Beta**) | v1 deprecated tutorial still linked; primary v2 workflows per migration guide (**Not verified** canonical list URL here) | **Not found in official docs** in material retrieved here | **Not verified** here | **Not verified** here | DC bulk historical path; Activities serviceGroup (**Not found** exhaustive verb/table list here without schema package) | **Not verified** | **NO** under ACC webhooks list reviewed | Migrate per [Forms Migration Guide](https://aps.autodesk.com/en/docs/acc/v1/overview/migration-guides/forms-v1-to-v2). | [overview — Forms tutorials](https://aps.autodesk.com/en/docs/acc/v1/overview/), [migration guide](https://aps.autodesk.com/en/docs/acc/v1/overview/migration-guides/forms-v1-to-v2) |
| Photos | Photos API (Forma APIs) | `POST …/photos:filter` (see `photos-getfilteredphotos-POST` in reference TOC) — **exact host/path Not verified from retrieved body** here | **Not found** in retrieved excerpt | **Not verified** | **Not verified** | DC activities cite Docs/Issues/Admin verbs ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | **Not verified** | **NO** in webhooks taxonomy reviewed | Reference entries are linked from TOC ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)). | [overview TOC](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Sheets | Sheets API (Forma APIs) | `GET …/collections` + sheet objects per reference TOC (**paths Not verified**) | **Not found** here | Uses collections + exports patterns per tutorials/overview | **Not verified** | **Not verified** | **Not verified** | **NO** in webhooks taxonomy reviewed | Export workflow via POST exports ([reference TOC link](https://aps.autodesk.com/en/docs/acc/v1/overview/)). | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Locations | Locations API v2 | `GET https://developer.api.autodesk.com/construction/locations/v2/projects/{projectId}/trees/{treeId}/nodes` (`treeId` must be **`default`**) | **NO** documented `updatedAt`/similar watermark in excerpt reviewed | **`nextUrl`** / `previousUrl` pagination + `limit` (1–10000, default 10000) + zero-based **`offset`** | Standard node model; deletes **Not found** here | DM “diff” unrelated; APS Webhooks lacks locations events in reviewed taxonomy | **Not verified** | **NO** in reviewed webhook events | Pagination details from reference mirror. | [locations nodes mirror](https://forge.autodesk.com/en/docs/acc/v1/reference/http/locations-nodes-GET), [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Assets | Assets API (Forma APIs) | `GET assets` endpoints per reference TOC (**URL Not retrieved**) | **Not found** here | **Not verified** | **Not verified** | Relationships API interoperability ([APS blog Relationships](https://aps.autodesk.com/blog/bim-360acc-relationships-api))—**Not** ingestion semantics | Per-app limits guidance is generic ([APS best-practices blog](https://aps.autodesk.com/blog/best-practices-developers-using-autodesk-platform-services-aps-apis)) — **rpm table Not verified** here | **NO** in reviewed taxonomy | Assets tutorials linked from overview. | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Cost (representative budgets list) | Forma Cost API v1 | Example pattern in docs: **`GET https://developer.api.autodesk.com/construction/cost/v1/projects/{projectId}/budgets`** (**verify exact plural segment** via HTTP reference)—**Not pinned** via retrieved GET body here | **Not found** in retrieved Cost GET pages here | Typical collection pagination pattern **Not verified** here without explicit cite | Entities emit **`.deleted-1.0`** webhook events (**soft signal**) | APS Webhooks `*.created\|updated\|deleted` for budgets, contracts, COR, RFQ (**Note:** spelled **`rfq`**, not RFC), etc. ([hub](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/cost_management_events)) | Endpoint tables exist for Data Management OSS/DM—not Cost-specific excerpt here (**Not verified**) | **YES** Cost Management webhook family | Separate **project initialization** webhook `project.initialized-1.0`. | Webhooks taxonomy: [cost events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/cost_management_events); Cost reference tree: [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Reviews | Forma Reviews API v1 | `GET workflows`, `GET reviews`, `GET versions`, `GET approval-statuses` enumerated as read-only GA endpoints ([reviews GA blog](https://aps.autodesk.com/blog/autodesk-construction-cloud-reviews-api-general-availability)) (**full URLs Not verified** beyond blog sample base) | **Not found** here | **Not verified** | **Workflow status** semantics | APS Webhooks **`review.created-1.0`, `review.closed-1.0`** ([reviews events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/reviews_events)) | **Not verified** | **YES** (`review.*`-1.0) | SSA unsupported ([reviews GA blog](https://aps.autodesk.com/blog/autodesk-construction-cloud-reviews-api-general-availability)). | [reviews GA blog](https://aps.autodesk.com/blog/autodesk-construction-cloud-reviews-api-general-availability), [reviews webhooks hub](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/reviews_events) |
| Docs / BIM 360 & Forma project files | Data Management API v2 + BIM 360/Forma extensions | Example entry listing: **`GET https://developer.api.autodesk.com/project/v1/hubs/{hub_id}/projects/{project_id}/topFolders`** (+ folder `contents`) | **`lastModifiedTime` fields** returned on folders (attribute on resources) vs query filter “since” watermark — **PARTIAL**/not same as transactional `updatedAt` filters | Mixed JSON:API resource documents + relationship `links`; folder contents paging per DM reference tables | Trash / `hidden` semantics + `excludeDeleted` query on `topFolders` ([topFolders GET](https://aps.autodesk.com/en/docs/data/v2/reference/http/hubs-hub_id-projects-project_id-topFolders-GET)) | APS Webhooks **Data Management events** incl. **`dm.folder.*`, `dm.version.*`, `dm.lineage.updated`, `dm.operation.*`** ([taxonomy](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/data_management_events)) | **YES** — per endpoint **rpm** matrices published ([DM limits](https://aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting/dm-rate-limits)) scoped **per OAuth client/app per endpoint** | **YES** (see events list) | **Folder-scoped webhook** supported — `scope.folder` urn pattern illustrated in webhook list doc ([systems hooks GET](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). | DM topFolders: [APS ref](https://aps.autodesk.com/en/docs/data/v2/reference/http/hubs-hub_id-projects-project_id-topFolders-GET), limits: [dm-rate-limits](https://aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting/dm-rate-limits), events: [data_management_events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/data_management_events) |
| ACC Files (“Files tool” operations) | Forma Files subgroup | Reference includes PDF export endpoints, packages, linked RCM files, naming standards (beta) per overview TOC | **Not verified** incremental filters | Mixed job-based export + GET packages | **Not verified** here | Shares DM events where files live | **Not verified** | **Indirect** via DM webhooks where applicable | Tutorials enumerated from overview sidebar. | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Insight / Bulk historical admin+collab data | BIM 360 / Forma **Insight → Data Connector** (not standalone Issues-style REST ingestion API in findings here) | `POST https://developer.api.autodesk.com/data-connector/v1/accounts/{accountId}/requests` (**example** payload in activities blog shows pattern) ([activities blog snippet](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | **`startDate` / `endDate` — activities only**, max **31‑day window**, **≤12‑month history** rolling default rules ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | Job queue produces CSV/ZIP bundles per jobs API | Deletes via snapshot diff / activities verbs — operational pattern | Dedicated **`activities` serviceGroup** in DC payload ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | **429** surfaced for quota/rate interplay ([jobs nested GET](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET)); **24 jobs rolling window** (**separate** from HTTP RPM) described in blogs (see §2). | **PARTIAL** — distinct from per-module ACC webhooks; BIM 360 DC intro lists “set up notification using webhooks” without operational detail ([BIM DC blog](https://aps.autodesk.com/blog/bim-360-data-connector-api)) | Does not replace Issues/Cost/etc. APS Webhooks families; notification semantics **Not verified** beyond marketing bullet. | [activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities), [BIM DC blog](https://aps.autodesk.com/blog/bim-360-data-connector-api), [GET jobs under request](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET) |
| Activities / Audit-style feed | Insight Data Connector | See Insight row (`serviceGroups` includes `"activities"`) ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | Applies **via `startDate`/`endDate` only on activities extracts** ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | N/A CSV job output | Event verbs vary by module (**verb catalog in README inside extract**, per Q&A ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities))) | This *is* the activity surface (no separate APS REST `/activities` list found across cited docs) | `filter[field_to_filter]` query typo noted (`fielter`) in APS blog quoting GET jobs/requests enhancements — consume live docs (**Not verified** typo fixed) ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | **NO** APS Webhooks equivalents for generalized ACC audit parity | Modules called out explicitly: **Docs, Issues, Account Admin** plus general language “service modules” ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)). Product UI help cross-links from that blog are **outside** the APS-only authority set for this artifact. | [activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities) |
| Hub / Project Admin | Hub Admin (Forma APIs) | Reference tree lists **Projects / Companies / Hub Users / Project Users / Business Units** ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)); exact primary collection URLs **`Not verified`** in retrieved rows | **Not found** | **Not verified** | **Not verified** | Administrative activity via DC **`activities`** support statement ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)) | **Not verified** | **NO** ACC-specific admin events in APS Webhooks excerpt reviewed | **Not found** SSA statement here (unlike Reviews GA blog). | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| BuildingConnected bid pipeline | BuildingConnected (+ APS Webhooks slice) | **Not researched** REST list endpoint citations in this artifact | — | — | — | Webhooks: **`opportunity.*`, `bid.created`** ([buildingconnected_events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/buildingconnected_events)) | **Not researched** REST rpm here | **YES** BuildingConnected webhook family only | Separate from unified Forma Issues namespace. | [buildingconnected_events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/buildingconnected_events) |
| Model Coordination & Clashes | Model Coordination subgroup | Tutorial + reference entries for Model Sets / Clashes ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)) | **Not verified** watermark | **Not verified** | **Not verified** | Operational events **Not found** in ACC webhooks reviewed | **Not verified** | **NO** in reviewed excerpt | Companion to Model Properties usage. | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Model Properties (Index query + Diff) | Model Properties subgroup | Tutorial shows **query + diff workflows** (**Not a transactional Issues list**) | Query language timestamps within scope of index/diff (**Not same as Issues `updatedAt`**) | Query-based | Deletion semantics tied to modeled diff ingestion | **Tutorial named “Tracking Changes”** implies diff ingestion approach ([tutorial link from overview](https://aps.autodesk.com/en/docs/acc/v1/tutorials/model-properties/diff)) | **Not verified** | MD webhooks (**`extraction.*`**) unrelated to transactional ACC modules ([model_derivative_events](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/model_derivative_events)) | Enables **hosted model change tracking**, not spreadsheet modules. | [diff tutorial TOC link](https://aps.autodesk.com/en/docs/acc/v1/tutorials/model-properties/diff), [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Relationships | Relationships API | Search + mutate endpoints enumerated in TOC | **Unknown** watermark list parity | Specialized search/sync posts | Deletes via delete relationships POST (**semantic type differs**) | Separate from transactional entity feeds | **Not verified** | **NO** in reviewed excerpt | Frequently paired with Assets per ecosystem blog ([relationships ecosystem blog](https://aps.autodesk.com/blog/bim-360acc-relationships-api)). | [relationships tutorials](https://aps.autodesk.com/en/docs/acc/v1/tutorials/relationships/relationships-tutorial) |
| AutoSpecs | AutoSpecs subgroup | TOC lists project summaries/registers/logs | **Not verified** | **Not verified** | **Not verified** | **Unknown** | **Not verified** | **NO** in reviewed excerpt | — | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Takeoff | Takeoff subgroup | **`GET …/projects/{project_id}/packages/{package_id}/takeoff-items`** (+ types/settings) paths appear in TOC | **Not found** watermark | Limit/offset **Not retrieved** here | **Not verified** | **Unknown** public `/activities` | **Not verified** | **NO** in reviewed excerpt | Package-scoped ingestion. | [takeoff refs in overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Transmittals | Transmittals subgroup | `GET transmittals` family in TOC (**URL Not pinned**) | **Not verified** | **Not verified** | **Not verified** | References **Change History** section label in changelog UI | **Not verified** | **NO** in excerpt | Listed at bottom of TOC. | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Weather | Weather subgroup | Exists as API Reference heading only in overview grab | — | — | — | — | — | — | **Not researched** endpoints here. | [overview](https://aps.autodesk.com/en/docs/acc/v1/overview/) |
| Viewer markups | **Not found as ACC REST slice** here | Community-only hits in web search (**Not official**) | — | — | — | Model Coordination workflows **Not verified** parity | — | — | No APS reference row located in enumerated Forma API Reference TOC for “markups” under this artifact’s cites. | _(none authoritative found)_ |

\* **Markups**: **Not found in official docs** enumerated in ACC Forma API Reference sidebar captured from ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)); requires dedicated Viewer/Markups documentation outside this excerpt if it exists.

### Additional APS webhook systems (outside ACC transactional modules reviewed above)

Listed exactly as APS Webhooks taxonomy enumerates adjacent systems referenced from the Supported Events navigator ([events via hooks GET breadcrumb list](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)): **Fusion Lifecycle**, **Revit Cloud Worksharing**, **Model Derivative**. Event names include `model.publish`, `model.sync`, `extraction.updated`, `extraction.finished`, Fusion `item.*` / `workflow.transition` hooks—see APS pages under each folder in the taxonomy.

---

## 2. Data Connector

### Overview + reference anchors

| Topic | Facts (with cites) |
|---|---|
| Formal documentation entry | Forma (**ACC v1**) Developer's Guide + API Reference nest **Data Connector** under Requests / Jobs / Data ([overview TOC](https://aps.autodesk.com/en/docs/acc/v1/overview/)). Cross-compatible BIM 360 wording appears in tutorials & jobs REST page ([tutorial pointer](https://aps.autodesk.com/en/docs/acc/v1/tutorials/data-connector/dc-tutorial-retrieve-data-extract)). |
| Public marketing / gateway blurb | `https://aps.autodesk.com/apis-and-services/data-connector-api` links editorially to APS blog titles including **Specific Limit for Jobs…** ([page](https://aps.autodesk.com/apis-and-services/data-connector-api)). |
| BIM 360-era introduction (still cites features) | Public beta GA narrative: CSV + `Readme.html` schema packaging, downloadable zip, **`30-day` availability**, notification via webhooks (feature bullet—**lifecycle detail Not verified**) ([blog](https://aps.autodesk.com/blog/bim-360-data-connector-api)). Authentication note (historical ACC/BIM rollout): **`3-legged` only** plus **Executive Overview** permission emphasis ([same blog §Note](https://aps.autodesk.com/blog/bim-360-data-connector-api)). Jobs listing doc later documents **OAuth code or SSA** yielding user context bearer token (**SSA semantics** described) ([nested jobs GET excerpt](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET)). |
| Tutorial headers (permission pattern) | “Token’s authenticated user must have executive overview permissions” listed in prerequisites for extracting data ([tutorial scaffold](https://aps.autodesk.com/en/docs/acc/v1/tutorials/data-connector/dc-tutorial-retrieve-data-extract)). |

### Daily quota (user “24/day” hypothesis)

**Verified:** Blogs state **limits of 24 jobs per rolling 24h window enforced both at account-level and individual user-level**, counting UI-triggered jobs + API jobs jointly; **`429`** when exceeded; distinct from generic HTTP RPM (**blog cites ACC rate-limits/data-connector page for higher RPM—see below**) ([quota blog](https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api)).  
**Recommendation text** in quota blog suggests ~**ONE_TIME per hour on average** to spread load ([quota blog §ideal workflow](https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api)).  
Supporting cross-link confirms **explicit 24‑hour restriction** surfaced in FAQ-style updates ([feature blog](https://aps.autodesk.com/blog/acc-data-connector-api-updates-multiple-projects-support-filter-project-status-and-others)).

**HTTP RPM (Data Connector REST):** The quota blog distinguishes an HTTP limit **“(60/minute/app)”** pointing at `https://aps.autodesk.com/en/docs/acc/v1/overview/rate-limits/data-connector-rate-limits/` (**page body Not extracted** inside this artifact) ([quota blog linkage](https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api)).

### Snapshot vs activity tables (coverage)

Official sources confirm:

- **`"activities"`** added to permissible **`serviceGroups`** string for **`POST/PATCH` requests**, with activities-only date window semantics (**31 days**, rolling defaults, **`≥20 min` sync latency** caveat) ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)).
- Schema packaging concept: each entity may have multiple CSV tables; job zip includes `readme.html`, schema subtree, changelog narrative moved toward public endpoints ([schema blog intro](https://aps.autodesk.com/blog/data-connector-schema-now-public-endpoints)).

**Explicit machine-readable authoritative map** (module → `{snapshot_tables}` vs `{activity_tables}`) was **Not found** in extracted pages without loading schema JSON/HTML endpoints (**URLs for those REST endpoints Not captured verbatim** beyond statement they’re fully documented APIs after Dec 2023 blog & later update notes) ([schema blog](https://aps.autodesk.com/blog/data-connector-schema-now-public-endpoints), [updates blog referencing documentation](https://aps.autodesk.com/blog/acc-data-connector-api-updates-multiple-projects-support-filter-project-status-and-others)).

### Activity CSV schema primitives

APS activities blog directs readers to **`README.html` inside extraction output** plus variable **“activities verbs”** documentation; rejects inferring verb payload uniformity ([activities blog Q&A §verbs](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)).  
**Structured column canonical list:** **Not found in official docs** as inline text cited here—defer to bundled README/HTML or schema endpoint once URL resolved.

### Job lifecycle (submit → CSV ready → expiry)

| Stage | Facts |
|---|---|
| Queue latency | BIM DC blog warns newly created requests may delay before jobs visibility (“few minutes”) ([BIM DC blog](https://aps.autodesk.com/blog/bim-360-data-connector-api)). Activities blog adds **≥20 minutes** ingestion sync caveat for freshly created Docs uploads defaulting UTC-yesterday extraction ([activities blog A1](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)). |
| Job record fields | `status` enumerated `queued\|running\|complete`; `completionStatus` `success\|failed\|cancelled`; timestamps `startedAt`, `completedAt`; progress 0–100 ([nested GET reference excerpt](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET)). |
| Download TTL | BIM DC blog asserts dataset availability **same as UI: 30 days** ([BIM DC blog](https://aps.autodesk.com/blog/bim-360-data-connector-api)); activities FAQ reiterates question about expiry pointing to Docs help anchor ([activities blog Q3 link](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)). |

### Concurrent jobs / parallel bulk extract

**Not found in official docs** as explicit concurrency cap distinct from account/user job-count quota in excerpts used.

### Region constraints (EMEA/US operational differences beyond hosting)

Regional hosting flags exist for APS Webhooks region headers & DC rate doc cross-links (**content Not extracted** beyond mention “all regions” deploy for job-count policy) ([quota blog §July 2024](https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api)). Detailed **parity statement** comparing US vs EMEA Data Connector extract outputs:**Not found**.

### Authentication

Blog historically: **three-legged OAuth only** emphasis for BIM360 DC introduction ([BIM DC blog](https://aps.autodesk.com/blog/bim-360-data-connector-api)).  
Contrast: Jobs retrieval reference documents Bearer token sourced from **authorization code flow or SSA-generated “3‑leg preserving user context”** token narrative ([nested jobs GET](https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET)).

### Multi-project extracts

Updates blog: **`projectIdList` max 50** projects/request; **`projectStatus` filter**: `active \| archived \| both`; recurrent activities may use **`dateRange` limited sets** (“today/yesterday/past seven days/current month/last month”) ([updates blog bullets](https://aps.autodesk.com/blog/acc-data-connector-api-updates-multiple-projects-support-filter-project-status-and-others)).

---

## 3. Webhooks

### Official doc anchors

| Item | Source |
|---|---|
| Landing / API hub | APS Webhooks gateway + nested developer guides ([`/webhooks-api` marketing](https://aps.autodesk.com/webhooks-api)); REST reference rooted at `[Webhooks REST API Reference](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http)` (breadcrumb on e.g. [hooks list doc](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). |
| Supported events aggregator | APS groups events ([Supported Events hub link from navigation excerpt](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). |
| Rate limiting & quotas (management plane) | Per-endpoint **rpm**, plus **quota: max 1000 hooks / event type / scope**, duplicate callback prohibition ([webhooks rate limits](https://aps.autodesk.com/en/docs/webhooks/v1/developers_guide/rate-limits/webhooks-rate-limits)). |

### Exhaustive event inventory (APS navigator excerpt)

Copied names directly from APS navigation text captured from [GET systems/:system/events/:event/hooks](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET):

**Data Management:** `dm.operation.completed`, `dm.operation.started`, `dm.folder.(copied|copied.out|moved|moved.out|purged|deleted|modified|added)`, `dm.lineage.updated`, `dm.lineage.unreserved`, `dm.lineage.reserved`, `dm.version.(copied|copied.out|moved|moved.out|deleted|modified|added)` ([folder page list](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/data_management_events)).  
**Model Derivative:** `extraction.updated`, `extraction.finished`.  
**Revit Cloud Worksharing:** `model.publish`, `model.sync`.  
**Fusion Lifecycle:** `workflow.transition`, `item.update/unlock/release/lock/create/clone`.  
**Cost Management:** enumerated `*-1.0` triplets for `budget`, `budgetPayment`, `contract`, `cor`, `costPayment`, `expense`, `expenseItem`, `mainContract`, `mainContractItem`, `oco`, `pco`, `rfq`, `scheduleOfValue`, `sco`, `segmentValue` + `project.initialized-1.0`.  
**BuildingConnected:** `bid.created`, `opportunity.created`, `opportunity.status.updated`, `opportunity.comment.(created|updated|deleted)`.  
**ACC Issues:** `issue.created|updated|deleted|restored|unlinked-1.0`.  
**ACC Reviews:** `review.created-1.0`, `review.closed-1.0`.

(Each bullet page URL follows `https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/...` naming pattern enumerated in navigator.)

### Subscription model (`scope`)

| Fact | Evidence |
|---|---|
| Webhook objects carry `scope` object; documentation states events may constrain valid scopes (**example given:** folder urn + inheritance semantics for DM) ([systems hooks GET](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). |
| Issues webhooks scoped by **`project` id JSON field** (`"scope":{"project":"<GUID>"}`) ([issue webhook GA blog snippet](https://aps.autodesk.com/blog/webhook-api-acc-issue-released)). |
| Optional `hubId`, `projectId`, `hookExpiry` fields enumerated on webhook resource ([same reference](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). |
| Regions: `region`/`x-ads-region` enumerations (**US default**, `EMEA`, `AUS`(Beta), others) documented on webhook list operation ([regions table](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)). |

### HMAC / signature verification requirements

APS blog (**practice article**) outlines: subscriber registers **`POST tokens`** secret; callbacks include **`X-Adsk-Signature`**; validation uses **`HMAC` with `SHA-1`** hexdigest of raw JSON body keyed by subscriber secret (**token length guideline 32–64 alnum** commentary) ([practice blog](https://aps.autodesk.com/blog/practice-payload-signature-webhook)); official tutorial landing exists at **[How to verify payload signature](https://aps.autodesk.com/en/docs/webhooks/v1/tutorials/how-to-verify-payload-signature)** (page body Not re-fetched verbatim here).

### Retry / failure policy for outbound callbacks

| Deterministic rule text | Availability |
|---|---|
| **Exact retry schedule / max attempts / backoff coefficients** — | **Not found in official docs** in sources extracted here beyond qualitative APS blog guidance on callback responsiveness, SSL, and firewall issues ([webhook reliability blog](https://aps.autodesk.com/blog/stopped-getting-webhook-callbacks)). |

### Payload sufficiency vs “fetch‑by‑id trigger” stance

**ACC Issues:** Blog states webhook callback includes **`changedAttributes`** for updates listing attribute names (**some fields remain coarse**, e.g. custom attribute edits only flagged generically until planned enhancements); created events include enumerated fields like subtype, assignee, due dates ([Issue webhook GA blog descriptive section](https://aps.autodesk.com/blog/webhook-api-acc-issue-released)).  
**ACC Reviews / Cost / DM / others:** Detailed per-event bodies **Not extracted** inside this artifact; APS hosts per-event spec pages (`…/reference/events/...`). Without fetching each leaf page:**Not found** to confirm universal full-row payloads—default integration posture should assume **verification + selective refetch**.

---

## 4. Findings worth flagging

- **Separate caps:** Insight/Data Connector ingestion must budget **both** REST **429** backoff behavior **and** the **distinct “24 extraction jobs per rolling 24 h per account AND per invoking user”** counter ([quota blog](https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api)). Designing L2=**~90 min recurrent DC activity pulls** clashes with coarse average guidance (~1 ONE_TIME/hour-style smoothing) absent deeper queue insight—simulate against live quota telemetry.
- **RFI ingestion shift:** Operational list operation is **`POST …/search:rfis`** not classic **GET paging** ([RFI v3 blog highlight](https://aps.autodesk.com/blog/autodesk-build-rfi-v3-api-released)); **No RFI APS Webhooks** enumerated in audited Supported Events taxonomy ([hooks nav excerpt](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)) → L1 watermark polling needs explicit filter discovery from POST search reference.
- **Reviews constraints:** SSA unusable ([reviews GA blog](https://aps.autodesk.com/blog/autodesk-construction-cloud-reviews-api-general-availability)); ingestion auth path must accommodate that limitation separately from Issues webhooks permitting SSA tokens ([Issues webhook GA blog](https://aps.autodesk.com/blog/webhook-api-acc-issue-released)).
- **Docs vs ACC modules:** Operational file mutations align with APS **DM webhooks**, not Issues—folder scope patterns become first-class ingestion triggers ([systems hooks GET](https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET)).
- **Cost naming:** APS Cost events label **`rfq.*`**, not “RFC”; map product vocabulary carefully ([cost events taxonomy](https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/cost_management_events)).
- **Activity extracts:** Verb heterogeneity enforced by APS guidance—bronze loaders must introspect **`README`/schema artifacts**, not infer fixed CDC columns ([activities blog verbs Q&A](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)).

---

## 5. Open gaps

- **RFC vs RFQ:** User asked RFC entities; APS Cost webhooks & docs explicitly list **`rfq.*`**. Separate RFC REST entity naming—**Not found in official docs** as distinct acronym in excerpts used.
- **Full authoritative map:** Data Connector **`serviceGroups → {snapshot vs activity CSV tables}`** without downloading schema bundles or locating **published schema/changelog HTTPS paths** verbatim—**partially documented** stating existence ([schema blog](https://aps.autodesk.com/blog/data-connector-schema-now-public-endpoints)) but URLs **Not pinned** inside artifact.
- **Concurrent extract policy** for multiple simultaneous jobs/project—**Not found**.
- **Per-module REST watermarks (`updated_at` equivalents)** beyond Issues + Submittals + partial DM attributes—mostly **Not found** pending individual HTTP leaf page ingestion.
- **ACC-native markups REST** ingestion—**Not found** enumerated in surveyed Forma API Reference TOC snapshot ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)).
- **Public Insight analytics REST besides Data Connector/UI exports** — **Not found** as separate enumerated API in cites used.
- **Construction schedule / Programme API** analogous to Primavera-style cloud endpoints—**Not found** inside Forma API Reference excerpt captured ([overview](https://aps.autodesk.com/en/docs/acc/v1/overview/)).
- **Webhook callback SLA math:** deterministic retry timelines—**Not found** (only experiential guidance blogs).
- **Whether every ACC endpoint emits `X-RateLimit-*` telemetry headers uniformly** APS blog asserts generic monitoring expectation ([APS best practices blog](https://aps.autodesk.com/blog/best-practices-developers-using-autodesk-platform-services-aps-apis)), but guarantees per service—**Not verified**.

### Exploratory (non-speculative labeling)

| Question | APS / Databricks official finding |
|---|---|
| ACC-wide CDC / transactional change-feed endpoints (non-model) besides DC activities? | **Not found** in surveyed REST navigation; Insight activities extract is nearest cited batch feed ([activities blog](https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities)). Model Properties **diff** caters to modeled elements ([tutorial link](https://aps.autodesk.com/en/docs/acc/v1/tutorials/model-properties/diff)). |
| Single GraphQL or multi-module bulk REST aggregator for ACC spreadsheets? | **Separate products:** APS markets **Data Exchange GraphQL** & **AEC Data Model GraphQL** (design-data emphasis) disconnected from enumerated Forma construction workflow REST modules ([graphql DX blog headline](https://aps.autodesk.com/blog/creating-data-exchanges-using-graphql-api)); no ACC “all modules one query” cite located here. |
| Data Connector **V2** announcement / delta vs **V1** | **Not found in official docs** during this scrape window (only incremental feature blogs). |
| Databricks Lakeflow Connect native Autodesk ACC source | **Not listed** among supported SaaS connectors in AWS Lakeflow SaaS roster ([Databricks SaaS connectors](https://docs.databricks.com/aws/en/ingestion/lakeflow-connect/saas-overview)). |
| Streaming transports (SSE / WebSockets) for ACC operational data | **Not found** in cited APS ACC REST references here. |
| Rate-limit telemetry headers globally | APS blog cites `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, plus honoring **429 backoff** philosophy ([APS best practices blog](https://aps.autodesk.com/blog/best-practices-developers-using-autodesk-platform-services-aps-apis)); unify with DM per-endpoint RPM tables ([dm-rate-limits](https://aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting/dm-rate-limits)). Uniform applicability statement per endpoint:**Not pinned**. |

**Community cross-check (non-authoritative):** Stack Overflow threads surfaced in incidental searches referencing Reviews base URL fragments—**Excluded** except as breadcrumb cues; cite only APS pages above.

---

## 6. Source index (flat URLs)

```
https://aps.autodesk.com/en/docs/acc/v1/overview/
https://aps.autodesk.com/en/docs/acc/v1/overview/migration-guides/forms-v1-to-v2
https://aps.autodesk.com/en/docs/acc/v1/tutorials/data-connector/dc-tutorial-retrieve-data-extract
https://aps.autodesk.com/en/docs/acc/v1/reference/http/issues-issues-GET
https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-requestId-jobs-GET
https://aps.autodesk.com/en/docs/data/v2/reference/http/hubs-hub_id-projects-project_id-topFolders-GET
https://aps.autodesk.com/en/docs/data/v2/developers_guide/rate-limiting/dm-rate-limits
https://aps.autodesk.com/en/docs/webhooks/v1/reference/http/webhooks/systems-system-events-event-hooks-GET
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/data_management_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/model_derivative_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/revit_cloud_worksharing_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/flc_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/cost_management_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/buildingconnected_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/issues_events
https://aps.autodesk.com/en/docs/webhooks/v1/reference/events/reviews_events
https://aps.autodesk.com/en/docs/webhooks/v1/tutorials/how-to-verify-payload-signature
https://aps.autodesk.com/en/docs/webhooks/v1/developers_guide/rate-limits/webhooks-rate-limits
https://aps.autodesk.com/webhooks-api
https://forge.autodesk.com/en/docs/acc/v1/reference/http/submittals-items-GET
https://forge.autodesk.com/en/docs/acc/v1/reference/http/issues-issues-GET
https://forge.autodesk.com/en/docs/acc/v1/reference/http/locations-nodes-GET
https://aps.autodesk.com/blog/bim-360-data-connector-api
https://aps.autodesk.com/blog/specific-limit-jobs-data-connector-api
https://aps.autodesk.com/blog/acc-data-connector-api-updates-multiple-projects-support-filter-project-status-and-others
https://aps.autodesk.com/blog/accbim-360-insight-data-connector-api-supports-activities
https://aps.autodesk.com/blog/data-connector-schema-now-public-endpoints
https://aps.autodesk.com/blog/autodesk-build-rfi-v3-api-released
https://aps.autodesk.com/blog/autodesk-build-submittals-api-general-availability
https://aps.autodesk.com/blog/webhook-api-acc-issue-released
https://aps.autodesk.com/blog/autodesk-construction-cloud-reviews-api-general-availability
https://aps.autodesk.com/blog/practice-payload-signature-webhook
https://aps.autodesk.com/blog/autodesk-platform-services-aps-api-rate-limits-best-practices-developers
https://aps.autodesk.com/blog/best-practices-developers-using-autodesk-platform-services-aps-apis
https://aps.autodesk.com/blog/stopped-getting-webhook-callbacks
https://aps.autodesk.com/blog/bim-360acc-relationships-api
https://aps.autodesk.com/blog/creating-data-exchanges-using-graphql-api
https://docs.databricks.com/aws/en/ingestion/lakeflow-connect/saas-overview
```

---

### Cross-reference note

Forge mirrors used where APS SPA shell blocked automated extraction (example Submittals, Issues duplicates): content URLs remain canonical sibling hosts per Autodesk (`forge.autodesk.com`).

