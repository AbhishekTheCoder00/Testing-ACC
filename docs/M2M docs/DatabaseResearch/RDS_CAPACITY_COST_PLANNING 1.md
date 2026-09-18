# RDS PostgreSQL + Secrets Manager — Capacity & Cost Planning

**Applies to:** `acc-connector/` production deployment (current schema + **future** M2M/SSA)  
**Companion docs:**
- [RDS_POSTGRESQL_GUIDE.md](./RDS_POSTGRESQL_GUIDE.md) — setup and migration
- [DATABASE.md](./DATABASE.md) — production schema (P1–P6)
- [M2M_SSA_IMPLEMENTATION.md](./M2M_SSA_IMPLEMENTATION.md) — hub-scoped SSA workflow
- [M2M_SSA_APS_CLIENT_SHARDING.md](./M2M_SSA_APS_CLIENT_SHARDING.md) — 10-robot APS quota

**Pricing basis:** AWS **us-east-1**, on-demand, approximate USD/month (**Aug 2026**).  
**Verify live:** [RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/), [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/), [AWS Pricing Calculator](https://calculator.aws/).

---

## Why AWS RDS PostgreSQL — and not other databases?

acc-connector today uses **SQLite** locally (`connector.db`). Production needs a **managed, relational** database that matches the same schema and Python code path. **Amazon RDS for PostgreSQL** is the chosen target ([RDS_POSTGRESQL_GUIDE §2](./RDS_POSTGRESQL_GUIDE.md), [DATABASE.md](./DATABASE.md)).

### What acc-connector needs from a database

| Requirement | Why |
|-------------|-----|
| **Relational tables + FKs** | `tenants`, `connections`, `ssa_credentials`, `sync_runs` with foreign keys and UNIQUE constraints |
| **Same engine as dev** | Code already uses `psycopg` + `CONN_STRING` toggle — no rewrite |
| **JSONB** (optional) | `sync_runs.record_counts` and similar metadata |
| **Small data volume** | Metadata only (~0.08–1.7 GB); secrets stay in **Secrets Manager**, not DB |
| **Managed ops** | Backups, patches, encryption, optional Multi-AZ failover |
| **VPC + SSL** | Production security for OAuth state, tokens, hub metadata |

### Database comparison

| Option | Fit for acc-connector? | Verdict |
|--------|------------------------|---------|
| **Amazon RDS PostgreSQL** | **Yes** | **Recommended.** Same PostgreSQL as dev; set `CONN_STRING`; AWS manages host, backups, patches. ~**$27–57/mo** instance at our scale. |
| **Aurora PostgreSQL** | Possible | **Overkill for now.** Higher cost (~$180+/mo at 100 hubs). Use only if you need Aurora Serverless, global DB, or many read replicas later. |
| **RDS MySQL** | Possible | **Not chosen.** Team spec and docs are PostgreSQL; would work technically but adds divergence from dev/SQLite migration path. |
| **Amazon DocumentDB** | **No** | MongoDB-compatible API — **wrong model.** Our schema is SQL tables, not documents. Full rewrite. |
| **Amazon DynamoDB** | **No** | Key-value / NoSQL — **wrong model.** No joins, FKs, or `sync_runs` history as we define it. Full rewrite. |
| **SQLite in production** | **No** | Single file on disk — no HA, no concurrent writes at scale, no managed backups. **Dev only.** |
| **Self-hosted PostgreSQL on EC2** | Possible | **Not recommended.** Saves ~$15/mo on VM; you own patches, backups, failover (~**8–16 hr/mo** ops). RDS is cheaper overall. |
| **Databricks / ACC as “database”** | **No** | Export files and API data live in Databricks volumes and ACC — **not** connector app state. |

### RDS PostgreSQL vs alternatives — one view

```text
                    acc-connector app state
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
    RDS PostgreSQL      DocumentDB /          SQLite file
    (SQL, managed)      DynamoDB              (dev only)
         │              (NoSQL)                    │
         │                    ✗ wrong schema        │
         ▼                                            ▼
   tenants,              would require          connector.db
   connections,           full rewrite           local laptop
   sync_runs...
         │
         +──── Secrets Manager (PEM, SP passwords) — NOT stored in RDS
```

### Why managed RDS (not only “PostgreSQL”)

| | **RDS PostgreSQL** | Self-hosted Postgres on EC2 |
|--|-------------------|----------------------------|
| Setup | Console / IaC, minutes | Install OS, Postgres, configure |
| Backups / PITR | Automated | You build and test |
| Minor version patches | AWS maintenance window | Your runbook |
| Disk scaling | gp3 autoscale | Manual |
| Multi-AZ failover | Optional checkbox | You design |
| **Typical ops time** | **~1–2 hr/mo** | **~8–16 hr/mo** |
| **Cost at 100 hubs** | **~$29/mo** (db.t4g.small) | ~$15 VM + your time |

### What RDS does NOT replace

Secrets (SSA PEM, Databricks SP passwords, APS client secrets) go to **AWS Secrets Manager** — see [DATABASE.md §2](./DATABASE.md) tiers T3/T4. RDS holds **metadata and refs only**.

**Bottom line for PM:** Use **RDS PostgreSQL** because our app is already built for it, data stays small, and alternatives are either **wrong architecture** (DocumentDB, DynamoDB) or **more ops cost** (self-hosted) without benefit at 100–2,000 hubs.

---

## Table of contents

0. [Why AWS RDS PostgreSQL — and not other databases?](#why-aws-rds-postgresql--and-not-other-databases)
1. [How to read scale tiers (100 → 2000)](#1-how-to-read-scale-tiers-100--2000)
2. [What future M2M adds to RDS and Secrets Manager](#2-what-future-m2m-adds-to-rds-and-secrets-manager)
3. [Storage model and formulas](#3-storage-model-and-formulas)
4. [AWS Secrets Manager cost model](#4-aws-secrets-manager-cost-model)
5. [Tier matrix — 100 to 2000 hubs](#5-tier-matrix--100-to-2000-hubs)
6. [RDS instance and machine recommendations](#6-rds-instance-and-machine-recommendations)
7. [APS Client ID sharding by tier](#7-aps-client-id-sharding-by-tier)
8. [Combined monthly cost summary](#8-combined-monthly-cost-summary)
9. [Operational levers (cost and capacity)](#9-operational-levers-cost-and-capacity)
10. [Recommendation — what is best for us and why](#10-recommendation--what-is-best-for-us-and-why)
11. [PM summary chart — full tier breakdown](#11-pm-summary-chart--full-tier-breakdown)

---

## 1. How to read scale tiers (100 → 2000)

acc-connector scales on **three counts**. This document uses **hub count** as the primary tier axis because production M2M keys everything to `hub_id` ([DATABASE.md §2](./DATABASE.md)).

| Term | Meaning | Drives cost most? |
|------|---------|-------------------|
| **Hub** (`tenant_id` = `hub_id`) | One ACC account / Forma hub | **Yes** — 1 SSA robot + 1 SM secret per hub |
| **Hub admin user** (`aps_user_id`) | Single person who onboarded the hub | Low — 1 row in `tenant_users` + U2M token |
| **Connection** (`connection_id`) | **One ACC project** + Databricks workspace + catalog | **Yes** — sync history + 1 DBX SP secret per project link |

A **connection** is not a user login — it is **one ACC project** wired to one Databricks workspace + catalog (`hub_id` + `project_id` + `dbx_workspace_url` + `catalog`). See [DATABASE.md §5.5–5.6](./DATABASE.md) and [M2M_SSA_IMPLEMENTATION.md §10–11](./M2M_SSA_IMPLEMENTATION.md).

### Two different secrets (do not mix them)

| Secret type | Scope | What it is | Count |
|-------------|-------|------------|-------|
| **SSA PEM (T3)** | **Per hub** | ACC robot private key | **1 per hub** — all projects in hub share same robot |
| **DBX SP secret (T4)** | **Per connection** | Databricks Service Principal OAuth password | **1 per connection** — when project + workspace + catalog needs its own SP ref |

Same hub, 4 ACC projects, **one workspace + one SP** → still **1 SSA PEM**, but the **schema stores one `client_secret_ref` per connection row** ([DATABASE.md T4](./DATABASE.md)). Multiple connections **may point to the same SP secret** (one SM entry, many refs) — you do **not** automatically need 4 SM entries unless each connection uses a **different** SP.

### Planning assumptions (industry-realistic)

| Assumption | Value | Rationale |
|------------|-------|-----------|
| **Hub admins (users)** | **1 per hub** | One hub admin onboards |
| **Connections per hub (budget)** | **1.5** (primary) | Realistic-low for multi-project SaaS; see scenarios below |
| Daily scheduled sync | **1 per connection** | ~30 `sync_runs` rows/month |
| Sync history retention | **12 months** | Configurable |
| M2M + SM | **Enabled** | Production path |
| APS robot quota | **10 per Client ID** | Sharding spec |
| RDS | **Single-AZ** unless noted | Multi-AZ ≈ 2× instance |

**Core formulas:**

```text
connections     =  hubs × connections_per_hub
ssa_secrets     =  hubs                         # 1 ACC robot PEM per hub
dbx_sp_secrets  =  connections                  # 1 SM entry per connection (may reuse same SP value)
aps_app_secrets =  ceil(hubs / 10)
platform        =  3

total_secrets   =  aps_app_secrets + ssa_secrets + dbx_sp_secrets + 3
sm_monthly      ≈  total_secrets × $0.40       # verify live AWS pricing
```

### Connections-per-hub scenarios (100 hubs example)

Fixed: APS apps = **10**, SSA PEMs = **100**, platform = **3**. Only **DBX SP count** varies.

| Scenario | Conn/hub | Connections | SP secrets | Total secrets | SM $/mo |
|----------|----------|-------------|------------|---------------|---------|
| **Optimistic floor** | 1.0 | 100 | 100 | **213** | **~$85** |
| **Realistic-low (budget here)** | 1.5 | 150 | 150 | **263** | **~$105** |
| **Realistic-high** | 2.5 | 250 | 250 | **363** | **~$145** |

**Industry range:** Budget **1.5–2.5 connections/hub** for production planning; use **1.0** as best-case floor only.

### Tier axis (primary plan @ 1.5 conn/hub)

| Tier (hubs) | Hub admins | Connections (×1.5) |
|-------------|------------|---------------------|
| **100** | 100 | **150** |
| **200** | 200 | **300** |
| **500** | 500 | **750** |
| **1,000** | 1,000 | **1,500** |
| **2,000** | 2,000 | **3,000** |

---

## 2. What future M2M adds to RDS and Secrets Manager

These items are **planned** (P1–P6 in [DATABASE.md §11](./DATABASE.md)) and included in every tier estimate below.

### 2.1 New RDS tables (metadata only — PEM stays out of RDS)

| Table | Per | Approx. row size |
|-------|-----|------------------|
| `aps_apps` | APS Client ID shard | ~300 B |
| `tenants` | Hub | ~500 B |
| `tenant_users` | Admin per hub | ~200 B |
| `ssa_credentials` | Hub (1:1) | ~800 B |
| `connections` | Connection | ~600 B |
| `connection_dbx_credentials` | Connection | ~400 B |
| `connection_bootstrap` | Connection | ~300 B |
| `sync_runs` | Each sync job | ~0.8–2 KB (JSONB) |
| `watermarks` | Connection | ~100 B |

Legacy tables (`acc_tokens`, `acc_config`, `dbx_tokens`, …) remain for U2M onboarding until fully deprecated.

### 2.2 Secrets Manager layout (from DATABASE.md §3)

```text
/{app}/prod/platform/aps-app-{app_ref}-secret     ← 1 per APS shard (every 10 hubs)
/{app}/prod/platform/data-encryption-key
/{app}/prod/platform/flask-secret-key             ← optional
/{app}/prod/hub/{hub_id}/ssa-private-key          ← 1 per hub (T3)
/{app}/prod/connection/{connection_id}/databricks-sp-secret  ← 1 per connection (T4)
```

**Critical:** At scale, **Secrets Manager monthly cost exceeds RDS** because each hub and connection gets its own secret ([RDS_POSTGRESQL_GUIDE §9](./RDS_POSTGRESQL_GUIDE.md)).

### 2.3 What does NOT grow with users

| Item | Storage |
|------|---------|
| Databricks export files | Databricks volumes / S3 — **not RDS** |
| SSA PEM bodies | Secrets Manager — **refs only in RDS** |
| ACC model data | ACC / DC API — **not RDS** |

---

## 3. Storage model and formulas

### 3.1 RDS data size (excluding backups)

```
connections = hubs × connections_per_hub    # budget: 1.5

metadata_MB =
  hubs × 1.3 KB + hubs × 0.2 KB + connections × 1.3 KB

sync_runs_GB =
  connections × 30 × 12 × 1.2 KB / 1_000_000

total_GB ≈ (metadata_MB / 1024 + sync_runs_GB) × 1.3
```

With **1.5 connections/hub**, 12-month retention:

| Hubs | Connections | sync_runs (12 mo) | **Total DB (est.)** |
|------|-------------|-------------------|---------------------|
| 100 | 150 | ~65 MB | **~0.08 GB** |
| 200 | 300 | ~130 MB | **~0.17 GB** |
| 500 | 750 | ~324 MB | **~0.42 GB** |
| 1,000 | 1,500 | ~648 MB | **~0.84 GB** |
| 2,000 | 3,000 | ~1.3 GB | **~1.7 GB** |

PostgreSQL handles this easily; **20 GB gp3** suffices through ~1,000 hubs at 1.5 conn/hub.

### 3.2 RDS allocated storage recommendation

| Tier (hubs) | Connections (×1.5) | Min gp3 | Autoscale max |
|-------------|----------------------|---------|---------------|
| ≤ 50 | ≤ 75 | **20 GB** | 30 GB |
| 100–500 | 150–750 | **20 GB** | 50 GB |
| 1,000–2,000 | 1,500–3,000 | **20 GB** | 100 GB |

Scale **disk** when `sync_runs` history grows (many projects × daily sync). Scale **instance class** when concurrent sync jobs or ECS tasks increase.

gp3 cost: **~$0.115/GB-month** → 20 GB ≈ **$2.30/mo**, 50 GB ≈ **$5.75/mo**.

Backup storage: first **100% of DB size** included; beyond that **~$0.095/GB-month**.

### 3.3 Secrets Manager storage

Each secret stores a small payload (PEM ~2–4 KB, OAuth secret ~100 B). SM pricing is **per secret**, not per KB — payload size barely matters.

---

## 4. AWS Secrets Manager cost model

| Charge | Approx. rate (us-east-1) |
|--------|--------------------------|
| Secret per month | **$0.40 / secret** |
| API calls | **$0.05 / 10,000 calls** |
| KMS (default AWS key) | Usually included in SM usage for standard keys |

### 4.1 Secret count — worked example (100 hubs)

```text
APS app secrets     10     ceil(100/10) — one client_secret per APS shard
SSA PEM secrets    100     one ACC robot key per hub
DBX SP secrets     150     one per connection (100 hubs × 1.5 conn/hub)
Platform secrets     3     DEK, Flask key, optional RDS password
                   ───
Total              263     × $0.40  =  $105.20/mo SM
```

At **optimistic floor** (1 conn/hub): `10 + 100 + 100 + 3 = 213` → **~$85/mo**.  
At **realistic-high** (2.5 conn/hub): `10 + 100 + 250 + 3 = 363` → **~$145/mo**.

```
connections       = hubs × connections_per_hub    # budget 1.5; range 1.0–2.5
aps_app_secrets   = ceil(hubs / 10)
ssa_secrets       = hubs
dbx_sp_secrets    = connections
platform_secrets  = 3

total_secrets = aps_app_secrets + ssa_secrets + dbx_sp_secrets + platform_secrets
sm_monthly    ≈ total_secrets × $0.40
```

**Per-hub SM (approx., at 1.5 conn/hub):** `(0.1 + 1 + 1.5) × $0.40` ≈ **$1.04/hub/mo** (+ APS shard step every 10 hubs).

### 4.2 API call estimate

Typical per scheduled sync (M2M path):

- 1× SSA private key read (cache miss or hourly token mint)
- 1× DBX SP secret read
- Occasional APS app secret read

**~2–3 GetSecretValue calls per sync per project connection.**  
150 connections × 30 syncs/month × 3 calls ≈ **13,500 calls/month** → **< $0.10** (negligible).

Token caching ([M2M_SSA_IMPLEMENTATION.md §5.2](./M2M_SSA_IMPLEMENTATION.md)) keeps API costs low.

---

## 5. Tier matrix — 100 to 2000 hubs

**Primary plan:** 1 hub admin/hub, **1.5 connections/hub**, M2M + SM, RDS Single-AZ.

SM formula: `ceil(hubs/10) + hubs + round(hubs × 1.5) + 3`.

### Tier A — 100 hubs (100 users, 150 connections)

| Item | Value |
|------|-------|
| SM secrets | 10 + 100 + 150 + 3 = **263** |
| SM cost | **~$105/mo** |
| RDS data | **~0.08 GB** |
| RDS instance | **db.t4g.small** |
| **Total** | **~$132–139/mo** |

*Floor (1 conn/hub): 213 secrets → **~$115/mo** total. High (2.5 conn/hub): **~$172/mo** total.*

---

### Tier B — 200 hubs (200 users, 300 connections)

| Item | Value |
|------|-------|
| SM secrets | 20 + 200 + 300 + 3 = **523** |
| SM cost | **~$209/mo** |
| RDS data | **~0.17 GB** |
| RDS instance | **db.t4g.small** |
| **Total** | **~$236–243/mo** |

---

### Tier C — 500 hubs (500 users, 750 connections)

| Item | Value |
|------|-------|
| SM secrets | 50 + 500 + 750 + 3 = **1,303** |
| SM cost | **~$521/mo** |
| RDS data | **~0.42 GB** |
| RDS instance | **db.t4g.medium** |
| **Total** | **~$573–588/mo** |

---

### Tier D — 1,000 hubs (1,000 users, 1,500 connections)

| Item | Value |
|------|-------|
| SM secrets | 100 + 1,000 + 1,500 + 3 = **2,603** |
| SM cost | **~$1,041/mo** |
| RDS data | **~0.84 GB** |
| RDS instance | **db.t4g.medium** |
| **Total (Single-AZ)** | **~$1,094–1,109/mo** |
| **Total (Multi-AZ)** | **~$1,144–1,174/mo** |

---

### Tier E — 2,000 hubs (2,000 users, 3,000 connections)

| Item | Value |
|------|-------|
| SM secrets | 200 + 2,000 + 3,000 + 3 = **5,203** |
| SM cost | **~$2,081/mo** |
| RDS data | **~1.7 GB** |
| RDS instance | **db.t4g.medium** |
| **Total (Single-AZ)** | **~$2,133–2,148/mo** |
| **Total (Multi-AZ)** | **~$2,183–2,213/mo** |

---

### Quick reference (budget @ 1.5 conn/hub)

| Hubs | Users | Connections | SM secrets | SM $/mo | RDS instance | **Total $/mo** |
|------|-------|-------------|------------|---------|--------------|----------------|
| **50** | 50 | 75 | 133 | $53 | db.t4g.micro | **~$69** |
| **100** | 100 | 150 | 263 | $105 | db.t4g.small | **~$135** |
| **200** | 200 | 300 | 523 | $209 | db.t4g.small | **~$240** |
| **500** | 500 | 750 | 1,303 | $521 | db.t4g.medium | **~$580** |
| **1,000** | 1,000 | 1,500 | 2,603 | $1,041 | db.t4g.medium | **~$1,100** |
| **2,000** | 2,000 | 3,000 | 5,203 | $2,081 | db.t4g.medium | **~$2,140** |

*Verify live: [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/) ($0.40/secret/month).*

---

## 6. RDS instance and machine recommendations

### 6.1 Instance classes (machine specs)

All instances use **AWS Graviton (ARM)** — `t4g` = burstable general purpose, lower cost than Intel `t3`.

| Instance | vCPU | RAM | max_connections (typ.) | gp3 storage | Instance $/mo | Use when |
|----------|------|-----|------------------------|-------------|---------------|----------|
| **db.t4g.micro** | 2 | **1 GB** | ~80–100 | 20 GB | **$12–15** | POC, ≤50 hubs, ≤75 connections |
| **db.t4g.small** | 2 | **2 GB** | ~150–200 | 20 GB | **$25–32** | Prod start, 100–500 hubs |
| **db.t4g.medium** | 2 | **4 GB** | ~300–400 | 20 GB | **$50–65** | 500+ hubs, Multi-AZ prod |
| db.t4g.large | 2 | 8 GB | ~500+ | 20–50 GB | $100–130 | Only if CPU/RAM pressure (unusual) |

Storage add-on: **20 GB gp3 ≈ $2/mo** (included in totals below).

### 6.2 Why smaller machine at lower tiers (and why not bigger)

| Question | Answer |
|----------|--------|
| **Why `micro` for POC (≤50 hubs)?** | DB data **< 0.05 GB**; app uses **~32 peak DB connections** (1 ECS task). Metadata-only workload — 1 GB RAM is enough. |
| **Why `small` at 100–500 hubs?** | DB data **0.08–0.42 GB**; still **≤64 connections** with 2 ECS tasks. Row count is tiny — bottleneck is ACC/DBX APIs, not PostgreSQL. |
| **Why `medium` at 500+ hubs?** | More scheduled syncs write `sync_runs`; headroom for **Multi-AZ** and admin UI under load. Still only **~1.7 GB** data at 2,000 hubs. |
| **Why NOT `large` by default?** | Paying **2–4×** for RAM/CPU you do not use. Data fits in **20 GB** disk. Upgrade only when CloudWatch shows **CPU > 70%** or **connections near limit**. |
| **Why NOT self-hosted EC2 Postgres?** | Saves ~$15/mo on hardware; costs **8–16 hr/mo** ops (patches, backups, failover). RDS includes that. |

**Rule:** Pick instance from **app connection pool + sync write rate**, not hub count or user count alone. **Users do not drive RDS size** — only connections (sync jobs) and ECS tasks do.

### 6.3 Scale path (1.5 conn/hub)

```text
≤50 hubs      → db.t4g.micro   (1 GB RAM)
100–500 hubs  → db.t4g.small   (2 GB RAM)
500–2000 hubs → db.t4g.medium (4 GB RAM)
```

**Prefer Graviton (`t4g`)** — lower cost than `t3` ([RDS_POSTGRESQL_GUIDE §6](./RDS_POSTGRESQL_GUIDE.md)).

### 6.4 Connection pool vs instance

acc-connector pool: **max 8 connections per app process** ([RDS_POSTGRESQL_GUIDE §5](./RDS_POSTGRESQL_GUIDE.md)).

```
required_connections ≈ gunicorn_workers × 8 × ecs_task_count
```

| Deployment | Workers | Tasks | Peak DB connections |
|------------|---------|-------|---------------------|
| Small prod | 4 | 1 | ~32 |
| Medium prod | 4 | 2 | ~64 |
| Large prod | 8 | 3 | ~192 |

At 2,000 hubs (~3,000 connections), RDS data ~**1.7 GB** — **medium** instance is sufficient unless many ECS tasks drive CPU up.

### 6.5 When to add Multi-AZ

| Stage | Multi-AZ? |
|-------|-----------|
| Dev / POC | **No** |
| Pilot (≤50 hubs) | **No** |
| Production (SLA, scheduled sync) | **Yes** — automatic failover |
| 500+ hubs | **Strongly yes** |

Multi-AZ doubles instance cost; storage is shared.

### 6.6 Aurora PostgreSQL?

| | RDS PostgreSQL | Aurora PostgreSQL |
|--|----------------|-------------------|
| Cost at 100 hubs (150 conn) | **~$135/mo total** | ~$180+ |
| Ops complexity | Low | Higher |
| Fit for acc-connector | **Yes — recommended** | Overkill until multi-region or read replicas |

Stick with **RDS PostgreSQL** until you need Aurora-specific features ([RDS_POSTGRESQL_GUIDE §2](./RDS_POSTGRESQL_GUIDE.md)).

---

## 7. APS Client ID sharding by tier

From [M2M_SSA_APS_CLIENT_SHARDING.md](./M2M_SSA_APS_CLIENT_SHARDING.md): **10 SSA robots max per APS Client ID**.

| Hubs | APS apps (`ceil(hubs/10)`) | SM platform secrets | Action before onboarding |
|------|----------------------------|---------------------|--------------------------|
| 1–10 | 1 (`app_a`) | 1 | Seed `app_a` at bootstrap |
| 11–20 | 2 | 2 | Register `app_b` **before hub 11** |
| 100 | 10 | 10 | Ops dashboard: monitor 8/10 threshold |
| 200 | 20 | 20 | Automate `aps_app_service.pick_app_with_capacity()` alerts |
| 500 | 50 | 50 | Consider APS quota increase request to Autodesk |
| 2,000 | 200 | 200 | Dedicated ops runbook; each app = separate MyApps registration |

**Each APS app** = separate Autodesk Developer Portal app + whitelist step for hub admins (they whitelist **their** hub’s assigned Client ID in UI).

Registering 200 APS apps at 2,000 hubs is an **operational** cost, not an AWS cost — plan quota increases with `ssa-requests@autodesk.com` ([M2M_SSA_IMPLEMENTATION.md §6.2](./M2M_SSA_IMPLEMENTATION.md)).

---

## 8. Combined monthly cost summary

### 8.1 Cost split by component (typical)

```text
At 100 hubs (150 conn):  RDS ~20%  |  Secrets Manager ~80%
At 500 hubs:             RDS ~8%   |  Secrets Manager ~92%
At 2000 hubs:            RDS ~3%   |  Secrets Manager ~97%
```

Budget **~$1.04/hub/mo** SM at 1.5 connections/hub.

### 8.2 Visual cost growth (1.5 conn/hub)

```text
Monthly USD (approx.)
│
│                                    ╭── 2000 hubs (~$2,140)
│                          ╭─────────╯ 1000 hubs (~$1,100)
│                ╭─────────╯
│      ╭─────────╯ 500 hubs (~$580)
│ ╭────╯ 200 hubs (~$240)
├─╯ 100 hubs (~$135)
└──────────────────────────────────────────────────► hubs
```

### 8.3 First-year ramp (1.5 conn/hub)

| Month | Hubs | Connections | RDS | Est. AWS/mo |
|-------|------|-------------|-----|-------------|
| 1–3 POC | 5 | 8 | micro | **~$22** |
| 4–6 pilot | 25 | 38 | micro | **~$42** |
| 7–12 | 75 | 113 | small | **~$110** |
| Year 2 | 200 | 300 | small | **~$240** |

---

## 9. Operational levers (cost and capacity)

| Lever | Saves | Trade-off |
|-------|-------|-----------|
| **Hub offboarding** | **$0.40/hub** (SSA) + **$0.40 × projects** (DBX secrets) | Full offboard per [DATABASE.md §9.3](./DATABASE.md) |
| **Disconnect one project** | **$0.40/mo** per removed connection | Delete connection row + SM secret |
| **sync_runs retention** (e.g. 12 mo vs 24 mo) | RDS storage (small $) | Less audit history |
| **Token/secret caching** in app | SM API calls (tiny $) | Must invalidate on rotation |
| **APS quota increase** (fewer shards) | Ops complexity, not SM $ | Autodesk approval |
| **Reserved Instances** (1-yr RDS) | ~30–40% on RDS portion | Commitment; SM stays on-demand |
| **Right-size RDS** | Avoid `large` if `medium` suffices | Monitor CPU/memory in PI |

**Cannot reduce:** 1 SSA secret per hub — required by security model (T3).

---

## 10. Recommendation — what is best for us and why

### 10.1 Short answer

| Phase | Hubs | Conn (×1.5) | **~Total $/mo** |
|-------|------|-------------|-----------------|
| POC | ≤50 | ≤75 | **~$22–69** |
| Prod (budget) | **100** | **150** | **~$135** |
| Growth | 200 | 300 | **~$240** |
| Scale | 500–2,000 | 750–3,000 | **~$580–2,140** |

**Budget SM at 100 hubs:** **$105–145/mo** (1.5–2.5 conn/hub).

### 10.2 Why RDS PostgreSQL (not DocumentDB, DynamoDB, or self-hosted EC2)

1. **Zero schema rewrite** — same tables, `psycopg`, `CONN_STRING` toggle ([RDS_POSTGRESQL_GUIDE](./RDS_POSTGRESQL_GUIDE.md)).
2. **M2M future schema** is relational (`tenants`, `connections`, FK constraints, UNIQUE on `service_account_id`) — fits PostgreSQL exactly ([DATABASE.md](./DATABASE.md)).
3. **Managed backups, patches, encryption** — cheaper ops time than EC2 Postgres.
4. **Data size** ~**0.08 GB at 100 hubs**, ~**1.7 GB at 2,000 hubs** (1.5 conn/hub) — **20 GB gp3** is enough for most tiers.

### 10.3 Why Secrets Manager (not RDS or S3 for secrets)

1. **Production spec mandates T3/T4 in SM** — PEM and Databricks SP never in RDS ([DATABASE.md DB-04](./DATABASE.md)).
2. **IAM task role integration** — ECS Fargate reads secrets without embedding in env ([DATABASE.md §2.1](./DATABASE.md)).
3. **Rotation and audit** — CloudTrail on `GetSecretValue`; optional rotation for platform secrets.
4. **Per-hub isolation** — aligns with `ensure_ssa(hub_id)` and immutable `private_key_ref` ([M2M_SSA_IMPLEMENTATION.md §7](./M2M_SSA_IMPLEMENTATION.md)).

Accept that **SM is the main bill** at scale; optimize via **offboarding**, not shared robots (forbidden).

### 10.4 Instance sizing — start minimal, scale on evidence

| Option | Verdict |
|--------|---------|
| **`db.t4g.micro`** | POC, ≤50 hubs |
| **`db.t4g.small`** | **100–500 hubs** (primary prod) |
| **`db.t4g.medium`** | **500+ hubs** or Multi-AZ |

SM scales with **connections** (DBX SP per connection row). Reuse one SP secret across connections in the same hub when security policy allows — reduces SM entries without changing SSA (always 1/hub).

### 10.5 Implementation order (aligned with DATABASE.md phases)

```text
P1  RDS + current schema + CONN_STRING          ← start here
P2  Secrets Manager backend (SecretStore)
P3  ensure_ssa + aps_apps sharding               ← before hub 11
P4  Replace m2m_service global .env path
P5  Session tenant_id + hub picker
P6  Remove legacy m2m_* tables
```

Do **not** delay P2/P3 if you expect >10 hubs — hub 11 blocks without `app_b` ([M2M_SSA_APS_CLIENT_SHARDING.md §6](./M2M_SSA_APS_CLIENT_SHARDING.md)).

### 10.6 What to monitor

| Metric | Alert when |
|--------|------------|
| `robot_count(app_ref)` | ≥ **8** per app (pre-hub-11 warning) |
| RDS CPU | > **70%** sustained 15 min |
| RDS `DatabaseConnections` | > **70%** of max |
| SM secret count | Tracks hub churn; drops on offboard |
| Sync failure rate | ACC/DBX API — likely fails before RDS |

### 10.7 Final verdict

**Best stack for acc-connector:**

```text
Amazon RDS PostgreSQL (micro → small → medium as hubs/projects grow)
        +
AWS Secrets Manager (1 SSA PEM/hub + 1 DBX SP/connection + APS shards)
        +
APS Client ID sharding (1 app per 10 hubs)
```

**Why:** SSA is **hub-scoped** (1 PEM). DBX SP secrets scale with **connections**; budget **1.5/hub** (~$105 SM at 100 hubs).

**Start:** `db.t4g.micro` (POC) → `db.t4g.small` at ~100 hubs (~**$135/mo** total).  
**Adjust** with §1 scenario table (1.0–2.5 conn/hub).

---

## 11. PM summary chart — full tier breakdown

**One-page view for planning.** Assumptions: **1 hub admin = 1 user**, **1.5 project connections/hub**, Single-AZ, us-east-1, M2M + Secrets Manager enabled.

### 11.1 Master table (hubs → cost)

| Tier | Hubs | Users | Conn | **RDS machine** | vCPU / RAM | RDS data | Disk | SM formula | SM secrets | SM $ | RDS $ | **Total $/mo** |
|------|------|-------|------|-----------------|------------|----------|------|------------|------------|------|-------|----------------|
| Pilot | **50** | 50 | 75 | **db.t4g.micro** | 2 / 1 GB | 0.04 GB | 20 GB | 5+50+75+3 | 133 | $53 | $17 | **~$69** |
| **A** | **100** | 100 | 150 | **db.t4g.small** | 2 / 2 GB | 0.08 GB | 20 GB | 10+100+150+3 | 263 | $105 | $29 | **~$135** |
| B | **200** | 200 | 300 | **db.t4g.small** | 2 / 2 GB | 0.17 GB | 20 GB | 20+200+300+3 | 523 | $209 | $29 | **~$240** |
| C | **500** | 500 | 750 | **db.t4g.medium** | 2 / 4 GB | 0.42 GB | 20 GB | 50+500+750+3 | 1,303 | $521 | $57 | **~$580** |
| D | **1,000** | 1,000 | 1,500 | **db.t4g.medium** | 2 / 4 GB | 0.84 GB | 20 GB | 100+1000+1500+3 | 2,603 | $1,041 | $57 | **~$1,100** |
| E | **2,000** | 2,000 | 3,000 | **db.t4g.medium** | 2 / 4 GB | 1.7 GB | 20 GB | 200+2000+3000+3 | 5,203 | $2,081 | $57 | **~$2,140** |

**SM formula key:** `APS + SSA + DBX + 3` = `ceil(hubs÷10) + hubs + connections + 3`  
**SM $** = secrets × **$0.40** · **RDS $** = instance + **~$2** storage

### 11.2 What each SM bucket is (example: 100 hubs)

| Bucket | Count | What it stores | Scales with |
|--------|-------|----------------|-------------|
| **APS** | 10 | Autodesk app `client_secret` (sharding) | 1 per 10 hubs |
| **SSA PEM** | 100 | ACC robot private key | **Hubs** |
| **DBX SP** | 150 | Databricks Service Principal password | **Connections** (projects) |
| **Platform** | 3 | Encryption key, Flask key, etc. | Fixed |
| **Total** | **263** | | **~$105/mo** |

### 11.3 Why this RDS machine per tier

| Tier | Machine | Why this size | Why NOT bigger |
|------|---------|---------------|----------------|
| 50 hubs | **micro** (1 GB) | POC; DB < 50 MB; ~32 app DB connections | **small** wastes ~$15/mo — no CPU/RAM need |
| 100–200 hubs | **small** (2 GB) | Prod; DB < 200 MB; metadata + sync_runs fit easily | **medium** adds ~$30/mo before 500 hubs |
| 500–2,000 hubs | **medium** (4 GB) | More sync writes; Multi-AZ headroom; still < 2 GB data | **large** adds ~$70/mo — no data-size reason |

**Users (hub admins) do not change RDS size.** 100 users on 100 hubs = same DB load as 100 hubs with 1 admin each.

### 11.4 Cost split (where the money goes)

| Tier | Hubs | SM % of total | RDS % of total | Main cost driver |
|------|------|---------------|----------------|------------------|
| 100 | 100 | **~78%** | ~22% | DBX SP + SSA secrets |
| 500 | 500 | **~90%** | ~10% | Connections × $0.40 |
| 2,000 | 2,000 | **~97%** | ~3% | SM secret count |

### 11.5 Visual — total monthly cost

```text
Total AWS $/month (RDS + Secrets Manager, Single-AZ)
│
│                                    ╭── Tier E: 2000 hubs  ~$2,140
│                          ╭─────────╯ Tier D: 1000 hubs  ~$1,100
│                ╭─────────╯ Tier C: 500 hubs    ~$580
│      ╭─────────╯ Tier B: 200 hubs     ~$240
│ ╭────╯ Tier A: 100 hubs      ~$135
├╯ Pilot: 50 hubs        ~$69
└────────────────────────────────────────────────────────────► hubs
   50    100    200    500    1000         2000
```

### 11.6 PM one-liner per tier

| Tier | Tell your PM |
|------|----------------|
| **100 hubs** | **~$135/mo** — `db.t4g.small` (2 GB), 263 secrets, 100 users, 150 project links |
| **200 hubs** | **~$240/mo** — same machine as 100 hubs; cost is mostly Secrets Manager |
| **500 hubs** | **~$580/mo** — step up to `db.t4g.medium` (4 GB); budget Multi-AZ for prod |
| **2,000 hubs** | **~$2,140/mo** — still one RDS instance (~1.7 GB data); SM is 97% of bill |

**Best default for production start:** Tier **A** — **100 hubs**, **db.t4g.small**, **~$135/mo** total.

---

## Related documents

| Document | Purpose |
|----------|---------|
| [RDS_POSTGRESQL_GUIDE.md](./RDS_POSTGRESQL_GUIDE.md) | Step-by-step RDS create + migrate |
| [DATABASE.md](./DATABASE.md) | Full DDL and secret tiers |
| [M2M_SSA_IMPLEMENTATION.md](./M2M_SSA_IMPLEMENTATION.md) | SSA workflow |
| [M2M_SSA_APS_CLIENT_SHARDING.md](./M2M_SSA_APS_CLIENT_SHARDING.md) | Hub 11+ sharding |

---

*Document version: 1.6 — Aug 2026. DB comparison at start; PM chart §11.*
