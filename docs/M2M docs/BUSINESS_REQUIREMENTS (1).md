# Forma → Databricks Connector — Business Requirements Document

**Version:** 1.0  
**Status:** Draft for review  
**Source:** Derived from [M2M_req 1.md](./M2M%20req%201.md)  
**Applies to:** Forma → Databricks Connector (`acc-connector/`)  
**Related documents:** [UI_DESIGN.md](./UI_DESIGN.md)

---

## 1. Executive Summary

The Forma → Databricks Connector enables Autodesk Construction Cloud (ACC) / Forma hub administrators to install and operate a repeatable data pipeline from ACC projects into Databricks Bronze tables, with ongoing scheduled synchronization.

This document defines the **business requirements** for multi-tenant identity, Secure Service Account (SSA) provisioning, and connection management. The connector must support enterprise customers who operate **multiple hubs**, **multiple hub administrators per hub**, and **multiple project-to-Databricks installations** — without duplicating robots, breaking tenant isolation, or requiring redundant onboarding steps.

**Access policy:** This SaaS is **for ACC/Forma hub administrators only**. Project admins, viewers, and other non–hub-admin ACC roles are **not** in scope as product users.

**Key business decision:** Each **Forma/ACC hub** is the isolation boundary for credentials, onboarding, and headless sync — not the broader ACC account. This ensures that hub administrators can independently onboard their hub without relying on setup performed in another hub under the same enterprise account.

---

## 2. Business Context

### 2.1 Problem Statement

Enterprise customers use ACC/Forma across multiple hubs and projects. They need a reliable, admin-operated connector that:

1. Lets authorized hub administrators set up data sync **once per hub** and reuse that setup for additional admins and connections.
2. Runs scheduled sync **without human login**, using machine-to-machine (M2M) credentials scoped to the correct hub.
3. Prevents credential sprawl, duplicate robot creation, and cross-hub data or access leakage.
4. Guides administrators through mandatory manual steps (Custom Integration whitelisting) with clear status and recovery paths.

Today’s proof-of-concept uses a single-user, global-credential model. Production must support **multi-tenant, hub-scoped operation** at scale.

### 2.2 Product Value

| Stakeholder | Value |
|-------------|-------|
| **Hub Admin** | One-time hub setup (whitelist + robot); shared connections across hub admin team |
| **Enterprise IT** | Predictable isolation per hub; auditable credential ownership |
| **Platform operator (vendor)** | Idempotent provisioning; fewer support incidents from duplicate robots |

### 2.3 Pipeline Overview

Each **connection** represents one installed pipeline:

```text
ACC / Forma project  →  Data Connector export  →  Bronze (Unity Catalog)  →  scheduled CDC
```

A connection is uniquely defined by: **hub + project + Databricks workspace + catalog**.

**Cardinality rule:** **One pipeline per catalog** — each Unity Catalog within a hub + project + workspace may have at most one sync pipeline. The system shall not create duplicate pipelines targeting the same catalog.

---

## 3. Business Objectives

| ID | Objective | Success indicator |
|----|-----------|-------------------|
| **BO-01** | Enable hub-scoped tenant isolation | Each hub has independent SSA, whitelist status, and connections |
| **BO-02** | Minimize redundant onboarding | Second admin on same hub does not create a new robot |
| **BO-03** | Support multi-hub administrators | Same person can administer multiple hubs with clear context switching |
| **BO-04** | Support multi-connection per hub | Same hub can sync multiple projects to same or different Databricks targets |
| **BO-05** | Enable reliable headless sync | Scheduled jobs use hub SSA, not individual user refresh tokens |
| **BO-06** | Preserve idempotent setup | Re-running setup for existing hub/connection resumes — does not duplicate resources |
| **BO-07** | Align with Autodesk SSA limits and policies | At most one robot per hub; respect 10-robot-per-Client-ID platform limit |
| **BO-08** | Restrict product access to hub admins | Non–hub-admin users cannot sign in or use the connector |

---

## 4. Scope

### 4.1 In Scope

- Multi-tenant identity model with **hub as tenant boundary**
- **Hub-admin-only** user access — authenticate and authorize ACC/Forma hub administrators only
- User-to-hub membership for hub admins (multiple hub admins per hub supported)
- SSA provisioning, storage, and reuse per hub
- Connection lifecycle: create, resume onboarding, operate sync, shared across hub admins
- Session context: logged-in user, active hub, active connection (when applicable)
- Onboarding status tracking at hub and connection levels
- Migration path from current POC (user-scoped session and global SSA) to production model

### 4.2 Out of Scope

- Automating Custom Integration whitelisting (no public API — manual admin action required)
- Cross-hub sharing of a single SSA robot (except explicit advanced/central-IT scenarios — not default)
- **Project admin, viewer, and other non–hub-admin ACC users** — not permitted to use this SaaS
- End-user (non-admin) ACC project member workflows
- Org-level billing or quota management (ACC account ID used for API routing only)
- Email/webhook notifications for failed runs (future product decision — see UI design open questions)

---

## 5. Stakeholders & Personas

**Primary user:** ACC/Forma **Hub Admin** only. All product flows — onboarding, connection setup, sync operation, and settings — are designed for and restricted to users with hub administrator privileges.

| Persona | Description | Primary needs |
|---------|-------------|---------------|
| **Hub Admin** | ACC hub administrator with Custom Integration and SSA setup rights | Whitelist Client ID, provision robot, add robot to projects, manage connections, operate sync |
| **Platform operator** | Internal support/engineering | Diagnose credential, onboarding, and sync failures |

### 5.1 Roles and Permissions (per hub)

Only **hub admins** may access the connector. Permissions apply within the hub the user administers.

| Action | Hub Admin (this hub) | Hub Admin (other hub) | Non–hub-admin user |
|--------|----------------------|------------------------|---------------------|
| Sign in and use the SaaS | ✅ | ❌ unless admin on that hub | ❌ |
| Whitelist Client ID in Custom Integration | ✅ | ❌ | ❌ |
| Provision SSA robot | ✅ | ❌ | ❌ |
| Add robot to ACC project | ✅ | ❌ | ❌ |
| Create / manage connections | ✅ | ❌ | ❌ |
| View dashboard and run sync | ✅ | ❌ unless also mapped | ❌ |

---

## 6. Key Business Concepts

Business terms map to underlying identifiers as follows. User-facing copy should prefer business terms (Hub, Connection, Project); technical IDs appear in secondary UI lines only.

| Business term | Meaning | Business rule |
|---------------|---------|---------------|
| **Hub** | A Forma/ACC hub — the tenant boundary | One SSA robot and one Custom Integration setup per hub |
| **Organization** | Optional grouping of hubs under one ACC account | Dashboard grouping only; does not merge tenant isolation |
| **Connection** | One pipeline installation: hub + project + Databricks workspace + catalog | **One pipeline per catalog** — owns bootstrap state, pipeline IDs, sync watermarks |
| **Hub setup** | Whitelist + SSA provisioning for a hub | Done once per hub; shared by all hub admins |
| **User mapping** | Link between a hub admin (Autodesk user) and a hub | Required for each hub admin who accesses the connector for that hub |
| **Headless sync** | Scheduled sync without human login | Always uses the hub’s SSA, not an individual user’s session |

### 6.1 Identifier Usage (business rules)

| Identifier | Business use |
|------------|--------------|
| **Hub ID** | Primary tenant key for SSA, whitelist, onboarding, and connections |
| **ACC account ID** | Data Connector API routing and optional org grouping — **not** the tenant key |
| **Connection ID** | Unique key for one sync installation and its operational state |
| **Autodesk user ID** | Identifies the logged-in human — session/login only |

**Rationale:** Large enterprises often have multiple hubs under one ACC account, each with different hub administrators. Using ACC account ID as the tenant key would incorrectly assume one admin and one robot covers all hubs.

---

## 7. Functional Requirements

### 7.1 Authentication & Session

| ID | Requirement |
|----|-------------|
| **FR-01** | The system shall authenticate users via Autodesk OAuth (3-legged). |
| **FR-01a** | After authentication, the system shall verify the user is a **hub administrator** for at least one hub before granting access. Users who are not hub admins (e.g., project admins only) shall be denied access with a clear message. |
| **FR-02** | The system shall maintain session context for: logged-in user, selected hub (tenant), and selected connection (when applicable). |
| **FR-03** | Users who administer multiple hubs shall select or switch hub context after login; all subsequent views and actions shall apply to the selected hub only. |
| **FR-04** | The system shall not use individual user refresh tokens for scheduled sync. |

### 7.2 User & Hub Membership

| ID | Requirement |
|----|-------------|
| **FR-05** | On first login to a hub, the system shall create a user-to-hub mapping if the user is a hub admin and mapping does not exist. |
| **FR-06** | Additional hub administrators joining an already-onboarded hub shall be mapped to the hub without triggering new SSA creation. |
| **FR-07** | The system shall store membership for **hub admins only** per hub (`role: hub_admin`). Project admin and viewer roles are not supported. |
| **FR-08** | A hub admin may be mapped to multiple hubs independently. |

### 7.3 Hub Tenant & SSA Provisioning

| ID | Requirement |
|----|-------------|
| **FR-09** | The system shall create at most **one SSA robot per hub**. |
| **FR-10** | SSA provisioning shall be idempotent: if a robot already exists for the hub, the system shall reuse it. |
| **FR-11** | The system shall not call the SSA creation API again for a hub that already has stored credentials. |
| **FR-12** | SSA credentials shall be stored securely (vault) and associated exclusively with the hub tenant. |
| **FR-13** | If a robot exists but is not yet added to a project, the system shall guide the admin to add the existing robot — not create a new one. |
| **FR-14** | The system shall track hub onboarding status: pending whitelist → whitelist verified → SSA active. |

### 7.4 Custom Integration & Whitelisting

| ID | Requirement |
|----|-------------|
| **FR-15** | The system shall detect whether the vendor Client ID is whitelisted for the hub’s Custom Integration. |
| **FR-16** | Until whitelisting is verified, the system shall block progression to project/Databricks setup and display actionable guidance for the hub admin. |
| **FR-17** | Whitelisting is a **manual** step performed by the hub admin in ACC — the connector shall not assume it can be automated. |

### 7.5 Connections

| ID | Requirement |
|----|-------------|
| **FR-18** | A connection shall be uniquely identified by hub + ACC project + Databricks workspace URL + catalog. |
| **FR-19** | Creating a connection for an existing combination shall resume from stored onboarding/sync state — not restart bootstrap from scratch. |
| **FR-20** | A new ACC project or new Databricks destination within the same hub shall create a **new** connection. |
| **FR-21** | All hub admins for a hub shall see and share the same connections for that hub. |
| **FR-22** | The system shall track connection onboarding status: pending custom integration → pending SSA → pending Databricks → pending bootstrap → ready. |
| **FR-23** | Each connection shall maintain its own pipeline IDs, bootstrap state, watermarks, and sync run history. |
| **FR-30** | The system shall enforce **one pipeline per catalog**: for a given hub + ACC project + Databricks workspace, each Unity Catalog shall have at most one connection (sync pipeline). Attempts to create a duplicate shall resume the existing connection. |

### 7.6 Sync Operation

| ID | Requirement |
|----|-------------|
| **FR-24** | Onboarding and interactive operations may use user OAuth (U2M) where appropriate. |
| **FR-25** | Scheduled headless sync shall authenticate to ACC using the hub’s SSA (M2M JWT-bearer). |
| **FR-26** | Scheduled headless sync shall authenticate to Databricks using a service principal (M2M client credentials) per connection/workspace policy. |
| **FR-27** | Sync failures shall be attributable to a specific connection and run phase without breaking idempotency on retry. |

### 7.7 Concurrency & Data Integrity

| ID | Requirement |
|----|-------------|
| **FR-28** | Simultaneous SSA provisioning attempts for the same hub shall result in exactly one robot (unique constraint + transactional create). |
| **FR-29** | The system shall enforce uniqueness: one tenant per hub, one user mapping per hub-user pair, one connection per connection ID, one SSA credential set per hub, and **one pipeline per catalog** per hub + project + workspace. |

---

## 8. Business Rules

| ID | Rule |
|----|------|
| **BR-01** | **Hub is the tenant boundary** for SSA, whitelist, onboarding, and connection ownership. |
| **BR-02** | **ACC account ID is not the tenant key** — it is used for Data Connector API paths and optional org-level grouping only. |
| **BR-03** | **One robot per hub** — never per user, never per connection. |
| **BR-04** | **New user on existing hub** → add user mapping only; reuse existing SSA and connections. |
| **BR-05** | **Existing user on new hub** → new hub tenant, new SSA, new connections for that hub. |
| **BR-06** | **Same user, second hub** → separate tenant context; hub picker required after login. |
| **BR-07** | **Do not recreate SSA** when a second hub admin completes onboarding for an already-provisioned hub. |
| **BR-08** | **Scheduled sync never uses human refresh tokens** — only hub SSA + Databricks service principal. |
| **BR-09** | Respect Autodesk limit of **10 service accounts per Client ID**; request increases via `ssa-requests@autodesk.com` when needed. |
| **BR-10** | Robot email is for **project invitation by the customer admin** — not used in API authentication. |
| **BR-11** | **One pipeline per catalog** — each Unity Catalog (within a hub + ACC project + Databricks workspace) shall have at most one sync pipeline. A catalog is not shared across multiple connections; duplicate pipeline installs to the same catalog are not permitted. |
| **BR-12** | **Hub-admin-only SaaS** — only ACC/Forma hub administrators may sign in and use the connector. Project admins, viewers, and other non–hub-admin roles shall not be granted product access. |

---

## 9. User Scenarios

### 9.1 Scenario A — First hub admin, new hub

**Actor:** Hub Admin (first time for this hub)  
**Preconditions:** User has ACC hub admin rights; Custom Integration not yet configured  

**Flow:**
1. User signs in with Autodesk OAuth.
2. User selects hub (only hub or first of several).
3. System creates hub tenant and user mapping.
4. System guides user to whitelist Client ID in Custom Integration.
5. After verification, system provisions SSA robot for the hub (once).
6. User selects ACC project and Databricks workspace/catalog.
7. System creates connection, runs bootstrap, enables sync.

**Outcome:** Hub has one SSA, one or more connections, user mapped as hub admin.

---

### 9.2 Scenario B — Second admin joins existing hub

**Actor:** Hub Admin B (Alice already completed setup)  
**Preconditions:** Hub tenant and SSA exist; connections may exist  

**Flow:**
1. Bob signs in with Autodesk OAuth.
2. Bob selects the same hub.
3. System adds Bob to hub user mapping — **does not** create new SSA.
4. Bob sees existing connections or continues wizard from hub onboarding status.

**Outcome:** Two user mappings, one SSA, shared connections.

---

### 9.3 Scenario C — One admin, multiple hubs

**Actor:** Hub Admin managing Hub A and Hub B  
**Preconditions:** User is admin on both hubs  

**Flow:**
1. User signs in; system presents hub picker (“You admin 2 hubs — set up which one?”).
2. User selects Hub A → sees Hub A SSA status and connections.
3. User switches to Hub B → session tenant context changes → Hub B SSA and connections load independently.

**Outcome:** Separate tenants, separate robots, separate connection sets per hub.

---

### 9.4 Scenario D — Same hub, new project or Databricks target

**Actor:** Hub Admin  
**Preconditions:** Hub SSA active; at least one connection exists  

**Flow:**
1. User selects “New connection.”
2. User picks a different ACC project and/or Databricks workspace/catalog.
3. System creates new connection ID; runs bootstrap for new target.
4. Existing connections remain unchanged.

**Outcome:** One hub SSA reused; multiple independent connections.

---

### 9.5 Scenario E — Resume interrupted onboarding

**Actor:** Hub Admin  
**Preconditions:** Partial onboarding stored on hub or connection  

**Flow:**
1. User signs in and selects hub.
2. System reads `onboarding_status` on tenant and/or connection.
3. User resumes from last incomplete step (whitelist, Databricks auth, bootstrap, etc.).

**Outcome:** No duplicate robots or connections; progress preserved.

---

### 9.6 Scenario F — Different hub admins under same ACC account

**Actor:** Hub A admin and Hub B admin (different people)  
**Preconditions:** Both hubs under same enterprise ACC account  

**Flow:**
1. Hub A admin completes whitelist + SSA for Hub A.
2. Hub B admin must independently complete whitelist + SSA for Hub B.
3. Hub B admin cannot rely on Hub A’s onboarding.

**Outcome:** Validates hub-scoped tenant model — ACC account grouping does not collapse onboarding.

---

## 10. Onboarding Decision Logic (Business View)

When a user completes login and selects a hub, the system shall evaluate three independent questions:

| Step | Question | If yes | If no |
|------|----------|--------|-------|
| **A** | Is this user already mapped to this hub? | Existing login for hub | Create user mapping |
| **B** | Does this hub already have a tenant + SSA? | Reuse SSA — do not create robot | Create hub tenant + provision SSA |
| **C** | Does this connection (hub + project + Databricks target) already exist? | Resume dashboard / sync | Create new connection + bootstrap |

**Important:** “New user” and “new hub” are independent dimensions. All four combinations (new/existing × new/existing) must be supported.

---

## 11. Non-Functional Requirements

| ID | Category | Requirement |
|----|----------|-------------|
| **NFR-01** | Security | SSA private keys stored in secure vault; not in application config or logs |
| **NFR-02** | Security | Tenant isolation — users cannot access another hub’s connections without hub-admin mapping on that hub |
| **NFR-03** | Reliability | Idempotent provisioning under concurrent admin actions |
| **NFR-04** | Compliance | Align with Autodesk SSA GA policies and tenant isolation guidance for ISV integrations |
| **NFR-05** | Operability | Onboarding and sync states visible to admins (aligned with UI design Pipeline Strip concept) |
| **NFR-06** | Scalability | Model supports multiple hubs per org, multiple connections per hub, multiple users per hub |

---

## 12. Data Entities (Business View)

| Entity | Business description | Cardinality |
|--------|---------------------|-------------|
| **Organization** | Optional enterprise grouping by ACC account | 1 org → many hubs |
| **Hub (tenant)** | Isolation boundary for credentials and connections | 1 hub → 1 SSA, many connections, many users |
| **Hub user** | Hub admin authorized to access connector for a hub | Many hub admins per hub |
| **SSA credentials** | Headless ACC robot for a hub | Exactly 1 per hub |
| **Connection** | One pipeline installation (one per catalog per project + workspace) | Many per hub; at most one per catalog |
| **Sync run** | Execution history for a connection | Many per connection |

---

## 13. Constraints & Assumptions

### 13.1 Constraints

- Custom Integration whitelisting has **no public API** — must remain a guided manual step.
- Autodesk default: **10 SSA robots per vendor Client ID** (increase via support request).
- Data Connector API requires ACC account ID in URL paths — derived from hub ID where applicable.
- SSA robot must be **invited to ACC projects** by a customer admin (robot email).

### 13.2 Assumptions

- Only **hub administrators** may sign in and use the connector; project admins are not product users.
- Hub administrators have sufficient ACC rights to configure Custom Integration and add service accounts to projects.
- Databricks workspace admins can authorize the connector’s service principal for M2M sync.
- Production deployment includes a secrets vault for per-hub SSA keys.
- UI will expose hub context switching for multi-hub admins (see UI_DESIGN.md).

---

## 14. Migration from Proof of Concept

| Current POC behavior | Required production behavior |
|----------------------|------------------------------|
| Session tracks user only | Session tracks user + hub (+ connection when set) |
| Per-user ACC config | Connections keyed by hub + project + Databricks target |
| Global SSA in environment config | Per-hub SSA in vault |
| Bootstrap state per user | Bootstrap state per connection |
| M2M tables isolated from tenant model | Align with hub tenant + connections model |

---

## 15. Success Criteria

| Criterion | Measure |
|-----------|---------|
| No duplicate robots per hub | Zero SSA create API calls when credentials already exist for hub |
| Second admin onboarding time | User mapping only — no repeat whitelist/SSA steps if already complete |
| Multi-hub admin usability | Hub switch loads correct connections without cross-hub leakage |
| Headless sync reliability | Scheduled runs succeed using hub SSA without user session |
| Connection reuse | Re-selecting same hub/project/Databricks target resumes — no duplicate bootstrap |
| Support incidents | Reduction in “robot limit exceeded” and “wrong hub credentials” tickets |
| Hub-admin-only enforcement | Non–hub-admin sign-in attempts rejected; no project-admin sessions created |

---

## 16. Dependencies & References

### 16.1 External dependencies

- Autodesk Platform Services — OAuth, SSA API, Custom Integration, Data Connector API
- Databricks — U2M OAuth (onboarding), M2M OAuth (scheduled sync)

### 16.2 Reference documents

- [M2M_req 1.md](./M2M%20req%201.md) — Technical architecture and decision record
- [UI_DESIGN.md](./UI_DESIGN.md) — UI specification aligned to this model
- [SSA goes GA](https://aps.autodesk.com/blog/update-secure-service-accounts-ssa-goes-ga)
- [Tenant Isolation for ISV Integrations](https://aps.autodesk.com/en/docs/ssa/v1/developers_guide/tenant-isolation-for-isv-integrations/)

---

## 17. Glossary

| Term | Definition |
|------|------------|
| **ACC** | Autodesk Construction Cloud |
| **SSA** | Secure Service Account — APS robot for headless M2M API access |
| **Custom Integration** | ACC hub setting where Client ID is whitelisted for API access |
| **M2M** | Machine-to-machine authentication (no interactive user) |
| **U2M** | User-to-machine authentication (interactive OAuth) |
| **Bootstrap** | Initial pipeline setup: export, Bronze ingest, baseline snapshot |
| **CDC** | Change data capture — incremental sync after baseline |
| **Bronze** | Raw ingested layer in Databricks Unity Catalog |
| **Connection** | One installed sync pipeline from ACC project to Databricks catalog |
| **Hub** | Forma/ACC hub — primary business tenant boundary |
| **Hub Admin** | ACC/Forma hub administrator — the only permitted SaaS user; project admins are not product users |

---

## 18. Requirements Traceability

| Business requirement | Source in M2M_req 1.md |
|---------------------|--------------------------|
| Hub as tenant key (BO-01, BR-01) | Identifier cheat sheet; Hub ID vs account ID |
| One SSA per hub (BO-02, FR-09) | SSA provisioning idempotency; Scenario 3 |
| Multi-hub admins (BO-03, FR-03) | Scenario 2; Session model |
| Connection uniqueness (BO-04, FR-18) | connection_id definition; Data model |
| One pipeline per catalog (BR-11, FR-30) | connection_id includes catalog; one Bronze pipeline per connection |
| Headless sync auth (BO-05, FR-25) | Auth paths table |
| Idempotent onboarding (BO-06) | Decision trees; Master decision tree |
| Hub-admin-only access (BO-08, BR-12, FR-01a) | Product scope decision — not in M2M_req 1.md |
| Onboarding enums (FR-14, FR-22) | Onboarding status enums |
| POC migration (Section 14) | Mapping to current POC |

---

## 19. Open Items for Product Sign-Off

1. **Organization label in UI** — Should hubs under the same ACC account show an org name in the hub switcher?
2. **Central IT exception** — Is sharing one robot email across multiple hubs ever a supported business scenario?
3. **Connection display naming** — Auto-name from project only, or allow custom connection names?
4. **Failed-run notifications** — Email/webhook alerts in v1 or deferred?
5. **Access-denied messaging** — Exact copy when a signed-in user is not a hub admin on any hub?

---

## 20. Approval

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Product owner | | | |
| Engineering lead | | | |
| Security / compliance | | | |
| Hub admin representative (customer) | | | |

---

*Document generated from technical requirements in M2M_req 1.md. For implementation detail, data model diagrams, and decision trees, refer to the source architecture document.*
