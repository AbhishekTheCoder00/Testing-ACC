# Amazon RDS PostgreSQL — Guide for ACC Connector

**Applies to:** `acc-connector/` Flask production deployment  
**Replaces:** `backend/repositories/connector.db` (SQLite) in production  
**Related:** [DATABASE.md](./DATABASE.md), [M2M_SSA_IMPLEMENTATION.md](./M2M_SSA_IMPLEMENTATION.md), [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md) (PM chart §11, 100–2000 hub tiers), `ssa-secrets-poc/`  
**AWS references:**
- [Amazon RDS for PostgreSQL](https://aws.amazon.com/rds/postgresql/)
- [AWS Databases overview](https://aws.amazon.com/products/databases/)
- [AWS Pricing Calculator](https://calculator.aws/)

---

## Table of contents

1. [What RDS replaces in acc-connector](#1-what-rds-replaces-in-acc-connector)
2. [Why RDS PostgreSQL (not DocumentDB / DynamoDB)](#2-why-rds-postgresql) — includes [managed vs self-hosted](#why-managed-rds-vs-self-hosted-open-source-postgresql-simple)
3. [Prerequisites](#3-prerequisites)
4. [Architecture after migration](#4-architecture-after-migration)
5. [Code changes in acc-connector (minimal)](#5-code-changes-in-acc-connector-minimal)
6. [AWS RDS setup — step-by-step (first time)](#6-aws-rds-setup--step-by-step-first-time)
7. [Connect acc-connector to RDS](#7-connect-acc-connector-to-rds)
8. [Migrate connector.db → RDS](#8-migrate-connectordb--rds)
9. [Cost analysis (current pricing model)](#9-cost-analysis-current-pricing-model) — see also [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md) for 100–2000 hub tiers
10. [User / hub capacity for our app](#10-user--hub-capacity-for-our-app)
11. [Operations you still own vs AWS manages](#11-operations-you-still-own-vs-aws-manages)
12. [Security checklist](#12-security-checklist)
13. [Troubleshooting](#13-troubleshooting)
14. [Future: production M2M schema](#14-future-production-m2m-schema)

---

## 1. What RDS replaces in acc-connector

### Current file (dev)

```text
acc-connector/backend/repositories/connector.db   ← SQLite file on disk
```

Selected when **`CONN_STRING` is not set** in `.env`.

Implementation: `backend/repositories/state/database.py`

### Production replacement

```text
Amazon RDS for PostgreSQL   ← managed PostgreSQL server in AWS
```

Selected when **`CONN_STRING` is set** — same Python code, same tables, same repositories.

### Tables in `connector.db` today

| Table | What it stores | Per-user / per-hub |
|-------|----------------|--------------------|
| `acc_tokens` | ACC OAuth tokens (encrypted) | Per ACC user (`user_id`) |
| `acc_config` | Selected hub, project, folder | Per user |
| `dbx_tokens` | Databricks OAuth tokens | Per user |
| `dbx_credentials` | Databricks PAT (legacy) | Per user |
| `app_secrets` | Databricks OAuth app creds | Per user |
| `bootstrap_state` | Bootstrap job IDs, paths | Per user |
| `watermarks` | Last sync timestamp | Per user + project |
| `sync_runs` | Sync state machine history | Per user + project |
| `catalog_claims` | Unity Catalog ownership | Per workspace + catalog |
| `m2m_config` | M2M POC config (legacy) | Global / config row |
| `m2m_bootstrap_state` | M2M bootstrap | Per config |
| `m2m_sync_runs` | M2M sync runs | Per config |
| `m2m_watermarks` | M2M watermarks | Per config |

**RDS stores the same rows** — not a different data model. Only the **engine** changes (SQLite → PostgreSQL).

### What RDS does NOT replace

| Component | Still separate |
|-----------|----------------|
| SSA private keys (PEM) | **AWS Secrets Manager** |
| APS client secret | **AWS Secrets Manager** |
| Flask `SECRET_KEY` | `.env` or Secrets Manager |
| Databricks export files | Databricks volumes / S3 |

See [DATABASE.md](./DATABASE.md) tier T1–T4.

---

## 2. Why RDS PostgreSQL

From [AWS RDS for PostgreSQL](https://aws.amazon.com/rds/postgresql/):

- Same **PostgreSQL** engine your code already supports via `psycopg`
- AWS manages: install, patches, backups, storage scaling, optional Multi-AZ
- VPC isolation, encryption at rest (KMS), SSL in transit
- Supports PostgreSQL 11–17

| AWS database | Use for acc-connector? |
|--------------|------------------------|
| **RDS PostgreSQL** | **Yes — recommended** |
| Aurora PostgreSQL | Yes, at higher scale/cost |
| DocumentDB | **No** — MongoDB API, wrong schema |
| DynamoDB | **No** — would require full rewrite |
| RDS MySQL | Possible but team spec is PostgreSQL |

### Why managed RDS vs self-hosted open-source PostgreSQL (simple)

> **We are not avoiding open source.** PostgreSQL **is** open source. **RDS = same PostgreSQL engine**, hosted and operated by AWS. Self-hosted = we install and run that same engine on our own EC2 server.

| Question | Simple answer |
|----------|---------------|
| Are we changing the database? | **No.** Same PostgreSQL tables, same Python code (`psycopg`). Only the **host** changes (SQLite file → RDS). |
| Why not run Postgres ourselves on EC2? | Cheaper VM (~$15/mo) but **we** own backups, patches, disk failures, and night-time outages. |
| What does RDS cost us? | ~**$13–32/mo** for POC/prod start — see [§9](#9-cost-analysis-current-pricing-model) and [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md). |
| What do we still do? | App schema migrations, `CONN_STRING`, security groups — see [§11](#11-operations-you-still-own-vs-aws-manages). |

**Reasons we chose RDS for acc-connector:**

1. **Already built for it** — flip `CONN_STRING` in `.env`; no app rewrite. Migration script exists: `scripts/migrate_sqlite_to_pg.py`.
2. **Production-ready in minutes** — create RDS in AWS console; self-hosted needs OS + Postgres install + backup scripts + monitoring.
3. **Automatic backups** — daily snapshots + point-in-time recovery if sync state or tokens are corrupted.
4. **Patches handled by AWS** — OS and PostgreSQL minor updates in a maintenance window; we do not SSH into a DB server.
5. **Same AWS home as the app** — Flask on ECS/EC2, secrets in **Secrets Manager**, DB in **VPC** on port 5432. One security model.
6. **Encryption built in** — data encrypted at rest (KMS) and in transit (`sslmode=require`).
7. **Optional failover** — Multi-AZ checkbox for prod; self-hosted failover is a custom project.
8. **Easy resize** — more RAM or disk when hubs grow (100 → 2,000); no manual disk migration on EC2.
9. **Monitoring included** — CloudWatch metrics (CPU, connections, storage) out of the box.
10. **Lower total cost** — RDS ~**$29/mo** vs EC2 ~**$15/mo** + **8–16 hr/mo** engineer time for ops ([RDS_CAPACITY_COST_PLANNING.md §0](./RDS_CAPACITY_COST_PLANNING.md)).
11. **Small data, high value** — we store OAuth state, sync history, hub metadata (~**< 2 GB** at scale). Losing it stops syncs for all hubs — reliability matters more than saving $15 on a VM.
12. **Secrets stay out of the DB** — SSA keys and Databricks passwords live in **Secrets Manager** ([DATABASE.md](./DATABASE.md)); RDS holds refs only. Same design with self-hosted or RDS — but RDS fits our AWS layout.

**What self-hosted would still require (our team):**

- Install and harden Linux + PostgreSQL on EC2
- Write and test backup + restore runbooks
- Plan and drill failover (or accept downtime)
- Monitor disk, CPU, connection limits manually
- On-call when the database server fails

**30-second answer for PM:**

> Use **Amazon RDS PostgreSQL** because acc-connector already supports it, production needs managed backups and uptime, and self-hosting saves ~$15/month but costs roughly **one full day of engineering time per month**. RDS is the same open-source PostgreSQL — AWS just runs it for us.

Full cost tiers (100–2,000 hubs): [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md).

---

## 3. Prerequisites

### AWS account

| Item | Required |
|------|----------|
| AWS account with billing enabled | Yes |
| IAM user or role with RDS create permissions | Yes |
| Region chosen (e.g. `us-east-1`) | Yes — same as Secrets Manager |
| VPC (default VPC is OK for POC) | Yes |

### IAM permissions (minimum for setup)

Your admin or dev user needs:

- `rds:CreateDBInstance`, `rds:DescribeDBInstances`, `rds:ModifyDBInstance`
- `ec2:DescribeVpcs`, `ec2:DescribeSubnets`, `ec2:CreateSecurityGroup`, `ec2:AuthorizeSecurityGroupIngress`

### acc-connector software (already in repo)

| Dependency | Location |
|------------|----------|
| `psycopg[binary]>=3.3.0` | `requirements.txt` |
| `psycopg-pool>=3.3.0` | `requirements.txt` |
| Dual backend | `backend/repositories/state/database.py` |
| Migration script | `scripts/migrate_sqlite_to_pg.py` |

### Network

| From | To | Port |
|------|-----|------|
| ECS / EC2 running Flask | RDS endpoint | **5432** |
| Your laptop (dev test only) | RDS | 5432 (restrict IP in security group) |

### Cost awareness

- RDS is **not free** forever (AWS Free Tier may apply for 12 months on eligible accounts — verify in console)
- Estimate with [AWS Pricing Calculator](https://calculator.aws/) before prod

---

## 4. Architecture after migration

```text
                    ┌─────────────────────────────────────┐
                    │  Users → ALB → Flask (gunicorn)     │
                    │  acc-connector on ECS/EC2           │
                    └──────────────┬──────────────────────┘
                                   │
                    CONN_STRING    │  IAM role
                    (PostgreSQL)   │  (Secrets Manager)
                                   ▼
         ┌─────────────────────────────┐    ┌──────────────────────────┐
         │  Amazon RDS PostgreSQL      │    │  AWS Secrets Manager     │
         │  Database: acc_connector     │    │  PEM, APS client secret  │
         │  ─────────────────────────  │    │  (NOT in RDS)            │
         │  acc_tokens                 │    └──────────────────────────┘
         │  acc_config                 │
         │  sync_runs                  │
         │  watermarks                 │
         │  … (same as connector.db)   │
         └─────────────────────────────┘

  DEV (unchanged):  CONN_STRING unset → connector.db (SQLite)
```

---

## 5. Code changes in acc-connector (minimal)

### No Python code changes required

Repositories, routes, and services already use:

```python
from backend.repositories import state_store as db
with db._conn() as con:
    con.execute('SELECT ...', (...))
```

`database.py` switches backend based on `CONN_STRING`.

### Only configuration changes

**Production `.env` (or ECS task env):**

```env
# REQUIRED for RDS — libpq connection string
CONN_STRING=host=acc-connector.xxxxxxxxx.us-east-1.rds.amazonaws.com port=5432 dbname=acc_connector user=acc_app password=YOUR_STRONG_PASSWORD connect_timeout=10 sslmode=require

# Flask (unchanged)
SECRET_KEY=...

# APS / Databricks (unchanged for U2M path)
APS_CLIENT_ID=...
APS_CLIENT_SECRET=...
```

**Local dev (keep SQLite):**

```env
# Leave CONN_STRING unset or empty
# Uses backend/repositories/connector.db automatically
```

### Connection pool (already configured)

```python
# database.py — max 8 connections per app instance
ConnectionPool(CONN_STRING, min_size=1, max_size=8, ...)
```

**Rule of thumb:** `RDS max_connections` should exceed `(gunicorn workers × 8 × number of app instances)`.

### Files you do NOT edit for basic RDS migration

| File | Change? |
|------|---------|
| `backend/repositories/state/*.py` | No |
| `app.py` | No |
| `backend/routes/*.py` | No |
| `requirements.txt` | No (psycopg already present) |
| `.env` | **Yes** — add `CONN_STRING` |
| Deploy script / ECS task def | **Yes** — inject `CONN_STRING` |

---

## 6. AWS RDS setup — step-by-step (first time)

### Step 1 — Open RDS console

1. Sign in to [AWS Console](https://console.aws.amazon.com/)
2. Region: **US East (N. Virginia) us-east-1** (or your standard region)
3. Search **RDS** → **Create database**

Direct: [RDS Create database](https://console.aws.amazon.com/rds/home)

---

### Step 2 — Engine and template

| Setting | Recommended (POC / small prod) |
|---------|--------------------------------|
| **Engine type** | PostgreSQL |
| **Engine version** | **16** or **17** (latest stable) |
| **Templates** | **Free tier** (if eligible) or **Dev/Test** |

---

### Step 3 — Settings

| Setting | Example value |
|---------|---------------|
| **DB instance identifier** | `acc-connector-dev` |
| **Master username** | `acc_admin` |
| **Master password** | Strong password (save in password manager) |

> Use a dedicated app user later; master user is for initial setup.

---

### Step 4 — Instance configuration

| Setting | POC / pilot | Small production |
|---------|-------------|------------------|
| **DB instance class** | `db.t4g.micro` (2 vCPU burst, 1 GB) | `db.t4g.small` (2 vCPU, 2 GB) |
| **Storage type** | gp3 | gp3 |
| **Allocated storage** | 20 GB | 20–50 GB |
| **Storage autoscaling** | Enable, max 100 GB | Enable |

Graviton (`t4g`) is lower cost; see [RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/).

---

### Step 5 — Availability and durability

| Setting | POC | Production |
|---------|-----|------------|
| **Multi-AZ** | **No** (save cost) | **Yes** (failover) |
| **Deletion protection** | No (dev) | **Yes** |

---

### Step 6 — Connectivity

| Setting | Value |
|---------|-------|
| **VPC** | Default VPC (POC) or app VPC (prod) |
| **Public access** | **No** for production; **Yes** only for temporary laptop testing |
| **VPC security group** | Create new: `acc-connector-rds-sg` |
| **Availability Zone** | No preference |
| **Database port** | `5432` |

**Security group inbound rule (after create):**

| Type | Port | Source |
|------|------|--------|
| PostgreSQL | 5432 | Security group of ECS/EC2 app **or** your office IP for dev |

---

### Step 7 — Database authentication

| Setting | Value |
|---------|-------|
| **Database name** | `acc_connector` |
| **Authentication** | Password |

---

### Step 8 — Monitoring and backup

| Setting | Recommended |
|---------|-------------|
| **Automated backups** | Enable |
| **Backup retention** | 7 days (POC), 14–35 days (prod) |
| **Encryption** | Enable (default AWS KMS key) |
| **Performance Insights** | Optional (POC off, prod on) |

---

### Step 9 — Create and wait

1. Click **Create database**
2. Wait **5–15 minutes** until status = **Available**
3. Copy **Endpoint** from the instance page, e.g.:

```text
acc-connector-dev.xxxxxxxxx.us-east-1.rds.amazonaws.com
```

---

### Step 10 — Create application database user (recommended)

Connect as master user (Query Editor v2 in RDS console, or `psql`):

```sql
CREATE USER acc_app WITH PASSWORD 'your_app_password';
GRANT ALL PRIVILEGES ON DATABASE acc_connector TO acc_app;
-- After tables exist (first app start):
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO acc_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO acc_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO acc_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO acc_app;
```

Use `acc_app` in `CONN_STRING`, not master user, in production.

---

## 7. Connect acc-connector to RDS

### From your laptop (test)

**PowerShell:**

```powershell
cd acc-connector
$env:CONN_STRING = "host=YOUR-RDS-ENDPOINT port=5432 dbname=acc_connector user=acc_app password=YOUR_PASSWORD connect_timeout=10 sslmode=require"
python -c "from backend.repositories.state import database; database.init_db(); print('Schema OK')"
```

### Production `.env`

```env
CONN_STRING=host=acc-connector-dev.xxxxx.us-east-1.rds.amazonaws.com port=5432 dbname=acc_connector user=acc_app password=STRONG_PASSWORD connect_timeout=10 sslmode=require
```

### Start app

```powershell
cd acc-connector
python app.py
# or
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

On first start, `init_db()` creates all tables with `CREATE TABLE IF NOT EXISTS`.

---

## 8. Migrate connector.db → RDS

One-time copy of existing dev data:

```powershell
cd acc-connector
$env:CONN_STRING = "host=... port=5432 dbname=acc_connector user=acc_app password=... sslmode=require"
python scripts/migrate_sqlite_to_pg.py
```

| Copied | Not copied |
|--------|------------|
| `acc_tokens`, `acc_config`, `sync_runs`, … | `app_secrets` (re-enter Databricks creds in UI) |

After migration:

1. Log in to connector UI
2. Re-connect Databricks per user if needed
3. Run a test sync
4. Do **not** delete `connector.db` until verified

---

## 9. Cost analysis (current pricing model)

> **Verify live prices:** [RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/) and [AWS Pricing Calculator](https://calculator.aws/).  
> Figures below are **approximate USD/month for us-east-1** (2025–2026 ballpark).

### RDS instance cost (Single-AZ, on-demand)

| Instance | vCPU / RAM | Approx. $/month | Use case |
|----------|------------|-----------------|----------|
| `db.t4g.micro` | 2 / 1 GB | **~$12–15** | Dev, pilot, ≤30 concurrent users |
| `db.t4g.small` | 2 / 2 GB | **~$25–32** | Small prod, ≤100 concurrent users |
| `db.t4g.medium` | 2 / 4 GB | **~$50–65** | Medium prod, heavy sync history |
| Multi-AZ (same class) | — | **~2× instance** | Production HA |

### Storage and backup (add-on)

| Item | Approx. cost |
|------|--------------|
| gp3 storage | **~$0.115 / GB-month** (20 GB ≈ $2.30) |
| Backup beyond free (100% of DB size free) | **~$0.095 / GB-month** |
| Data transfer out to internet | Variable (usually small for DB) |

### Example monthly totals (Single-AZ)

| Deployment | RDS instance | Storage 20GB | Est. total/month |
|------------|--------------|--------------|------------------|
| **Dev / POC** | db.t4g.micro | $2 | **~$14–18** |
| **Pilot (10 hubs)** | db.t4g.micro | $2 | **~$14–18** |
| **Small prod (50 users)** | db.t4g.small | $5 | **~$30–38** |
| **Medium prod (200 users)** | db.t4g.medium | $10 | **~$60–75** |
| **Prod + Multi-AZ** | db.t4g.small × 2 | $5 | **~$55–70** |

### Add Secrets Manager (M2M production — see [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md))

Formula: `total_secrets = ceil(hubs/10) + hubs + connections + 3` at **~$0.40/secret/month**.

| Hubs | Conn/hub | Connections | SM secrets | SM $/mo | + RDS small | **~Total** |
|------|----------|-------------|------------|---------|-------------|------------|
| 10 | 1.5 | 15 | 29 | $12 | $30 | **~$44** |
| 50 | 1.5 | 75 | 133 | $53 | $30 | **~$85** |
| **100** | **1.5** | **150** | **263** | **$105** | $30 | **~$135** |
| 100 | 1.0 (floor) | 100 | 213 | $85 | $30 | **~$115** |
| 100 | 2.5 (high) | 250 | 363 | $145 | $30 | **~$175** |

**100 = SSA PEMs (1/hub).** **150 = DBX SP secrets (1/connection @ 1.5 conn/hub).** **10 = APS app shards.** **3 = platform.**

SSA PEM + DBX SP secrets often exceed RDS cost — plan hub/project offboarding.

### AWS Free Tier (new accounts)

May include **750 hours/month** of `db.t2.micro` or `db.t3.micro` for **12 months**.  
Check **Billing → Free tier** in your account. `db.t4g.micro` may not be free tier eligible.

### Cost vs self-hosted EC2 PostgreSQL

| | RDS db.t4g.micro | EC2 t3.small + self Postgres |
|--|------------------|------------------------------|
| Instance | ~$13/mo | ~$15/mo |
| Your ops time | ~1–2 hr/mo | ~8–16 hr/mo |
| Backups / patches | Included | Your responsibility |

---

## 10. User / hub capacity for our app

### How acc-connector counts “users”

| Term | DB table | Typical row size |
|------|----------|------------------|
| **ACC human user** | `acc_tokens`, `acc_config` | ~2–5 KB |
| **Sync history** | `sync_runs` | grows over time (largest table) |
| **Hub (future M2M)** | `tenants`, `ssa_credentials` | ~1 KB metadata |

Database size is usually **small**; sync frequency and `sync_runs` retention matter most.

### Capacity by RDS size

| RDS tier | ACC users (metadata) | Hubs (future) | Concurrent Flask users | sync_runs (rough) |
|----------|---------------------|---------------|------------------------|-------------------|
| db.t4g.micro | 50–200 | 500+ | 5–15 | millions of rows OK with indexes |
| db.t4g.small | 200–1,000 | 2,000+ | 15–50 | same |
| db.t4g.medium | 1,000+ | 10,000+ | 50+ | same |

**Bottleneck is usually:**

- ACC / Databricks API rate limits
- Sync job duration
- **Not** PostgreSQL row count for typical connector metadata

### Connection pool limits

| App config | Value |
|------------|-------|
| Pool per instance | `max_size=8` |
| gunicorn workers | e.g. 4 |
| Connections per instance | up to ~32 if all workers busy |
| 2 ECS tasks | up to ~64 connections |

`db.t4g.micro` default `max_connections` ≈ **80–100** — fine for 1–2 app instances.

### Row growth estimate (per active user)

| Table | Rows per user per month (active sync) |
|-------|---------------------------------------|
| `sync_runs` | 30–300 (daily sync) |
| `watermarks` | 1–10 |
| `acc_tokens` | 1 |

**50 active users × 12 months × 100 sync_runs** ≈ 60,000 rows — trivial for PostgreSQL.

---

## 11. Operations you still own vs AWS manages

| Task | Self-hosted Postgres | RDS PostgreSQL |
|------|---------------------|----------------|
| Install PostgreSQL | You | AWS |
| OS patches | You | AWS |
| Postgres minor patches | You | AWS (maintenance window) |
| Postgres major upgrade | You | AWS (with planning) |
| Automated backups | You | AWS |
| Point-in-time recovery | You | AWS |
| Disk scaling | You | AWS (autoscale) |
| Multi-AZ failover | You | AWS (optional) |
| Set `CONN_STRING` | You | You |
| Schema migrations | You | You |
| Query tuning / indexes | You | You |
| Security group rules | You | You |

### PM quick reference — who owns what?

| If this happens… | Self-hosted Postgres | RDS PostgreSQL |
|------------------|---------------------|----------------|
| Disk is full | **We** expand EBS, maybe migrate data | **AWS** autoscale storage (gp3) |
| Postgres security patch | **We** schedule downtime and apply | **AWS** applies in maintenance window |
| Need backup from yesterday | **We** restore from our script (if it worked) | **AWS** point-in-time recovery |
| Server dies at 2 AM | **We** get paged, rebuild/fix | **Multi-AZ:** AWS fails over (optional) |
| App needs more DB connections | **We** tune `max_connections`, maybe bigger VM | **We** resize instance (few clicks) |
| Audit asks “who can access DB?” | **We** document EC2 + SG + IAM | **We** document SG + IAM; RDS logs in CloudWatch |

**Takeaway:** RDS does not remove **application** work (migrations, indexes, `CONN_STRING`). It removes **infrastructure babysitting** so the team focuses on connector features, not database servers.

---

## 12. Security checklist

| Item | Action |
|------|--------|
| Public access | **Off** in production |
| `sslmode=require` | In `CONN_STRING` |
| Encryption at rest | Enable on RDS |
| App DB user | `acc_app` with least privilege, not master |
| Security group | Only app SG → 5432 |
| Secrets in git | Never commit `CONN_STRING` password |
| PEM / client secret | Secrets Manager, not RDS |
| Deletion protection | Enable on prod instance |

---

## 13. Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `connection timed out` | SG blocks 5432 | Add inbound rule from app SG |
| `password authentication failed` | Wrong user/password | Reset in RDS or recreate user |
| `database "acc_connector" does not exist` | DB name mismatch | Create DB or fix `dbname=` in DSN |
| `SSL required` | RDS enforces SSL | Add `sslmode=require` |
| `too many connections` | Pool × workers × instances too high | Reduce workers or upgrade instance |
| App still uses SQLite | `CONN_STRING` empty | Set env var in prod deploy |
| Migration 0 rows | Empty connector.db | Normal if fresh dev DB |

### Test connectivity

```powershell
# Requires psql client
psql "host=ENDPOINT port=5432 dbname=acc_connector user=acc_app sslmode=require"
```

---

## 14. Future: production M2M schema

After RDS is running, apply additional DDL from [DATABASE.md](./DATABASE.md):

- `aps_apps`, `tenants`, `tenant_users`, `ssa_credentials`, `connections`
- Same RDS instance, new tables
- SSA PEM still in **Secrets Manager** (`private_key_ref` in RDS)

Migration phases (from DATABASE.md):

| Phase | Action |
|-------|--------|
| P1 | RDS + current schema (this guide) |
| P2 | Secrets Manager for M2M |
| P3 | `ensure_ssa(hub_id)` + sharding tables |
| P4 | Deprecate `m2m_*` tables |

---

## Quick reference — decision chart

| Question | Answer |
|----------|--------|
| What file does RDS replace? | `backend/repositories/connector.db` |
| Code changes? | **Only `CONN_STRING` in `.env`** |
| Python package needed? | Already in `requirements.txt` |
| Best instance to start? | **db.t4g.micro** (POC) → **db.t4g.small** (prod) |
| Multi-AZ for POC? | No |
| Public access for POC? | Temporary only; restrict IP |
| Migrate existing data? | `python scripts/migrate_sqlite_to_pg.py` |
| Where do PEM keys go? | **Secrets Manager** (not RDS) |
| Dev laptop? | Keep SQLite (no `CONN_STRING`) |

---

## Summary

1. **`connector.db` is SQLite for local dev** — one file, all app state tables.  
2. **RDS PostgreSQL is the production replacement** — same tables, same code path.  
3. **Enable RDS by setting `CONN_STRING`** — no repository code changes.  
4. **Follow Section 6** to create your first RDS instance in AWS.  
5. **Run `init_db()` then optional migration script** to move data.  
6. **Budget ~$115–175/month SM + ~$30 RDS** for 100 hubs (263 secrets @ 1.5 conn/hub); see [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md).  
7. **Capacity:** small RDS handles hundreds of users; sync API limits matter before DB limits.

---

*Document version: 1.0 — Aug 2026. RDS PostgreSQL setup guide for acc-connector.*
