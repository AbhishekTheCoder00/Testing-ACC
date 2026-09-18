# AWS Costing — Simple Guide (RDS + Secrets Manager)

**For:** PM, devops, first-time AWS setup  
**Applies to:** `acc-connector/` production (M2M + RDS PostgreSQL)  
**Detail tiers:** 1, 10, 50, 100 hubs  
**Verify live:** [RDS pricing](https://aws.amazon.com/rds/postgresql/pricing/), [Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/)  
**Related:** [RDS_POSTGRESQL_GUIDE.md](./RDS_POSTGRESQL_GUIDE.md), [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md)

---

## Table of contents

1. [One-page summary](#1-one-page-summary)
2. [Cost per 1 hub (1 connection)](#2-cost-per-1-hub-1-connection)
3. [The 7 secrets formula (1 hub)](#3-the-7-secrets-formula-1-hub)
4. [Hub tiers: 1, 10, 50, 100](#4-hub-tiers-1-10-50-100)
5. [RDS machine choice (T vs M class)](#5-rds-machine-choice-t-vs-m-class)
6. [Storage: why 20 GiB](#6-storage-why-20-gib)
7. [Secrets Manager — how it works](#7-secrets-manager--how-it-works)
8. [What must go in SM vs RDS](#8-what-must-go-in-sm-vs-rds)
9. [AWS console: secret types & RDS create options](#9-aws-console-secret-types--rds-create-options)
10. [Cost graph](#10-cost-graph)
11. [PM cheat sheet](#11-pm-cheat-sheet)
12. [1:1 costing, three ratios & when to upgrade RDS](#12-11-costing-three-ratios--when-to-upgrade-rds)
    - [12.5 Every row — when to use / not use](#125-every-row--rds-pick-when-to-use-when-not-to-use)
    - [12.6 Why micro vs small](#126-why-micro-vs-small--one-rule-per-scale)

---

## 1. One-page summary

```text
┌─────────────────────────────┐     ┌─────────────────────────────┐
│  RDS PostgreSQL             │     │  AWS Secrets Manager        │
│  Tables: hubs, syncs, refs  │     │  Vault: PEM, passwords      │
│  ~$17–29/mo (fixed machine) │     │  ~$0.40 × secret count      │
└─────────────────────────────┘     └─────────────────────────────┘
         App data                           Sensitive keys
```

| Scale | Users | Connections | RDS machine | RDS $/mo | SM secrets | SM $/mo | **Total $/mo** |
|-------|-------|-------------|-------------|----------|------------|---------|----------------|
| **1 hub** | 1 | 1–2 | db.t4g.micro | ~$17 | 6–7 | ~$2–3 | **~$19–20** |
| **10 hubs** | 10 | 15 | db.t4g.micro | ~$17 | 29 | ~$12 | **~$29** |
| **50 hubs** | 50 | 75 | db.t4g.micro | ~$17 | 133 | ~$53 | **~$69** |
| **100 hubs** | 100 | 150 | db.t4g.small | ~$29 | 263 | ~$105 | **~$135** |

**Assumptions:** 1 hub admin per hub, **1.5 project connections per hub**, Single-AZ, us-east-1, M2M enabled.

---

## 2. Cost per 1 hub (1 connection)

### Minimum case — 1 hub, **1 project connection**

| Component | Detail | Cost |
|-----------|--------|------|
| **RDS** | `db.t4g.micro`, 2 vCPU, 1 GB RAM, 20 GiB gp3 | **~$17/mo** |
| **Secrets Manager** | 6 secrets (see below) | **~$2.40/mo** |
| **Total** | | **~$19/mo** |

### Budget case — 1 hub, **1.5 connections** (rounds to 2)

| Component | Detail | Cost |
|-----------|--------|------|
| **RDS** | Same micro (fixed — same machine for 1 or 10 hubs) | **~$17/mo** |
| **Secrets Manager** | 7 secrets | **~$2.80/mo** |
| **Total** | | **~$20/mo** |

> **Per-hub SM cost** drops as you add hubs (platform secrets shared). **RDS cost** is **fixed** until you upgrade the machine (~50–100 hubs).

---

## 3. The 7 secrets formula (1 hub)

For **1 hub** with **2 project connections** (1 × 1.5 budget):

```text
 1   APS app secret       ceil(1÷10) = 1   →  Autodesk app password (platform)
 1   SSA PEM              1 per hub        →  ACC robot private key
 2   DBX SP secrets       2 connections    →  Databricks SP password per project
 3   Platform secrets     fixed            →  see table below
───
 7   total secrets  ×  $0.40  =  ~$2.80/month
```

### If only **1 project** connected (not 1.5)

```text
1 + 1 + 1 + 3 = 6 secrets  →  6 × $0.40 = ~$2.40/mo SM  →  ~$19/mo total
```

### What the “3 platform secrets” are

**Not 3 AWS products** — **3 shared keys for the whole connector app** (same 3 whether you have 1 hub or 100):

| # | Secret | Why needed |
|---|--------|------------|
| 1 | **Data encryption key (DEK)** | Encrypt OAuth tokens stored in RDS |
| 2 | **Flask secret key** | Secure user login sessions |
| 3 | **Third platform key** | Often RDS master password ref or extra platform config |

You **cannot remove** these without changing security design.

---

## 4. Hub tiers: 1, 10, 50, 100

### SM formula (all tiers)

```text
secrets = ceil(hubs/10) + hubs + connections + 3
connections = hubs × 1.5
SM $/mo = secrets × $0.40
```

### Tier A — 1 hub

| | Value |
|--|-------|
| Users | 1 |
| Connections | 2 (budget) or 1 (min) |
| RDS | **db.t4g.micro** (1 GB RAM) |
| RDS data | ~0.001 GB |
| SM | 7 (or 6) secrets |
| **Total** | **~$19–20/mo** |

### Tier B — 10 hubs

| | Value |
|--|-------|
| Users | 10 |
| Connections | 15 |
| RDS | **db.t4g.micro** (1 GB RAM) |
| SM | 1+10+15+3 = **29** → **~$12/mo** |
| RDS | **~$17/mo** |
| **Total** | **~$29/mo** |

### Tier C — 50 hubs

| | Value |
|--|-------|
| Users | 50 |
| Connections | 75 |
| RDS | **db.t4g.micro** (1 GB RAM) |
| SM | 5+50+75+3 = **133** → **~$53/mo** |
| RDS | **~$17/mo** |
| **Total** | **~$69/mo** |

### Tier D — 100 hubs (production budget)

| | Value |
|--|-------|
| Users | 100 |
| Connections | 150 |
| RDS | **db.t4g.small** (2 GB RAM) |
| RDS data | ~0.08 GB on **20 GiB** disk |
| SM | 10+100+150+3 = **263** → **~$105/mo** |
| RDS | **~$29/mo** |
| **Total** | **~$135/mo** |

### Machine + user detail (100 hubs)

| Dimension | Choice | Why |
|-----------|--------|-----|
| **Users** | 100 hub admins | 1 per hub — **does not drive RDS size** |
| **RDS class** | **Burstable T** (`db.t4g.small`) | Small metadata DB; not heavy CPU |
| **RAM** | 2 GB | Enough for ~64 app DB connections |
| **Storage** | 20 GiB gp3 | Actual data ~80 MB; 20 GB is AWS minimum + headroom |
| **Not** `db.m5.large` | 8 GB RAM, ~$130/mo | Overkill — paying for unused power |

---

## 5. RDS machine choice (T vs M class)

### Recommended burstable classes

| Stage | Instance | RAM | ~$/mo | Hubs |
|-------|----------|-----|-------|------|
| POC | **db.t4g.micro** | 1 GB | ~$14–17 | 1–50 |
| Prod | **db.t4g.small** | 2 GB | ~$28–29 | 100 |
| Scale | **db.t4g.medium** | 4 GB | ~$57 | 500+ |

**In AWS console:** select **Burstable classes (includes t classes)** — **not** Standard classes (m).

### T class vs M class — simple

| | **T = Burstable** (t4g, t3) | **M = Standard** (m5, m6) |
|--|----------------------------|---------------------------|
| **Analogy** | Small car — OK daily, brief speed bursts | Truck — full power all day |
| **CPU** | Baseline + burst credits | Full CPU always |
| **Cost** | **~$14–57/mo** | **~$130+/mo** |
| **acc-connector?** | **Yes — use this** | **No — too expensive** |

**Why T is enough:** Our DB is tiny (MB–GB), ~32–64 connections, sync metadata only — not a heavy analytics database.

---

## 6. Storage: why 20 GiB

| Question | Answer |
|----------|--------|
| How much data at 100 hubs? | **~0.08 GB** (~80 MB) |
| Why allocate 20 GiB? | **AWS minimum** for gp3; cheap (~$2/mo); huge headroom |
| Need 50 or 200 GiB? | **No** for our app |
| What grows storage? | **`sync_runs` history** (sync jobs), not secrets |

Secrets are **not** in RDS disk — they are in Secrets Manager.

---

## 7. Secrets Manager — how it works

### Not folders, not SQL tables

```text
RDS (database format):          Secrets Manager (key-value vault):

  tenants                          forma-connector/prod/hub/b.xxx/ssa-private-key
  ├── hub_id                       → value: -----BEGIN PRIVATE KEY-----
  ├── project_id
  └── sync_runs                    forma-connector/prod/connection/cnx_xxx/databricks-sp-secret
                                     → value: dapi1234secret...
  (SQL rows & columns)
                                   (one name → one secret string)
```

| | RDS | Secrets Manager |
|--|-----|-----------------|
| **Format** | Tables, rows, SQL | **Name → value** (like a labeled safe deposit box) |
| **Query** | `SELECT * FROM tenants` | `GetSecretValue("path/name")` |
| **Good for** | App data, millions of rows | Passwords, PEM keys (~hundreds of items) |
| **Cost** | Fixed machine ~$17–29/mo | **$0.40 per secret per month** |

### Why not store PEM/passwords in RDS tables to save money?

| Reason | Explanation |
|--------|-------------|
| **Security spec** | [DATABASE.md](./DATABASE.md): PEM and SP passwords **must not** be in RDS (T3, T4) |
| **Backup risk** | DB backup = copy of all secrets |
| **One leak = all hubs** | Table with 100 PEMs worse than 100 isolated SM entries |
| **Doesn’t save much** | SM ~$105 vs RDS ~$29 at 100 hubs — SM is the bill, but **cannot** move PEM to RDS per spec |
| **client_id is already in RDS** | Public IDs are free in tables — only **passwords/PEM** need SM |

### Can we use one SM secret (JSON) for all hubs?

**POC only.** One secret = $0.40/mo but **one breach exposes everything**. Production uses **separate secrets** per hub/connection.

---

## 8. What must go in SM vs RDS

| Item | In RDS? | In SM? | Negotiable? |
|------|---------|--------|-------------|
| APS **client_id** | **Yes** (public) | No | — |
| APS **client_secret** | Ref only | **Yes** | **No** (prod) |
| SSA **PEM** (robot key) | Ref only | **Yes** | **No** (prod) |
| Databricks **client_id** | **Yes** (public) | No | — |
| Databricks **SP password** | Ref only | **Yes** | **No** (prod) |
| Hub, project, sync_runs | **Yes** | No | — |
| Flask key, DEK | No | **Yes** | **No** |
| U2M OAuth tokens | Encrypted in RDS | Optional | Different path |

### Two secret types — do not mix

| Secret | Scope | Count at 100 hubs |
|--------|-------|-------------------|
| **SSA PEM** | **Per hub** (1 ACC robot) | **100** |
| **DBX SP password** | **Per connection** (per project link) | **150** (×1.5 conn/hub) |

Same hub, 3 projects → still **1 SSA PEM**, up to **3 DBX secrets** (or 1 if same SP reused).

---

## 9. AWS console: secret types & RDS create options

### Secrets Manager — “Secret type” screen

| Console option | Use for acc-connector? |
|----------------|------------------------|
| **Credentials for Amazon RDS database** | Optional — **PostgreSQL login password only** |
| DocumentDB / Redshift / Other DB | **No** |
| **Other type of secret** (API key, OAuth) | **Yes** — SSA PEM, APS secret, Databricks SP |

Pick **“Other type of secret”** for PEM and OAuth passwords.

### RDS — “Create database” options

| Option | Use? |
|--------|------|
| **Full configuration** | **Yes** — full control |
| Easy create | POC quick test only |
| Restore from S3 | **No** (unless migrating backup) |

### RDS settings checklist (POC → 100 hubs)

| Setting | POC (1–10 hubs) | Prod (100 hubs) |
|---------|-----------------|-----------------|
| Deployment | Single-AZ (1 instance) | Single-AZ (Multi-AZ later) |
| Template | Dev/Test | Dev/Test or Production |
| Instance class | **Burstable → db.t4g.micro** | **Burstable → db.t4g.small** |
| Storage | gp3, **20 GiB** | gp3, **20 GiB** |
| Public access | **No** (Yes only for temp laptop test) |
| Engine | PostgreSQL **16 or 17** | PostgreSQL **16 or 17** |

---

## 10. Cost graph

### Total monthly cost (RDS + Secrets Manager)

```text
Total $/month
│
│                                              ╭── 100 hubs (~$135)
│                                    ╭─────────╯
│                          ╭─────────╯ 50 hubs (~$69)
│                ╭─────────╯
│      ╭─────────╯ 10 hubs (~$29)
│ ╭────╯ 1 hub (~$20)
└──────────────────────────────────────────────────► hubs
  1     10      50      100
```

### Who pays the bill?

```text
1 hub:    RDS ████████████████  ~85%   SM ██  ~15%     (RDS fixed ~$17 dominates)
10 hubs:  RDS ██████████  ~59%        SM ███████  ~41%
50 hubs:  RDS ████  ~25%               SM ████████████  ~75%
100 hubs: RDS ██  ~22%                 SM ████████████████  ~78%
```

**Takeaway:** At **1 hub**, RDS feels expensive (fixed machine). At **100 hubs**, **Secrets Manager dominates** (~$105 of ~$135).

### SM vs RDS — pricing model

| | Secrets Manager | RDS |
|--|-----------------|-----|
| **Formula** | **$0.40 × secret count** | **Instance + disk** (fixed tier) |
| **Grows with** | Hubs + project connections | Almost flat until upgrade |
| **1 hub** | ~$3/mo | ~$17/mo |
| **100 hubs** | ~$105/mo | ~$29/mo |

---

## 11. PM cheat sheet

### Tell your PM in one line

> **~$20/month for 1 hub POC; ~$135/month for 100 hubs production. Most cost is Secrets Manager (~$0.40 per secret), not the database server.**

### Quick answers

| Question | Answer |
|----------|--------|
| Cost per 1 hub? | **~$19–20/mo** (micro RDS + 6–7 secrets) |
| Cost per 1 connection (extra project)? | **+$0.40/mo** (one DBX SP secret) |
| Why 7 secrets? | 1 APS + 1 PEM + 2 DBX + 3 platform |
| Why not secrets in DB tables? | **Security spec** — PEM/passwords in vault, not SQL |
| Why 20 GiB storage? | AWS minimum; we use ~0.08 GB at 100 hubs |
| Which RDS class? | **Burstable t4g** — micro (POC), small (100 hubs) |
| Why not m5.large? | **~$130/mo** — 10× cost, no benefit for our workload |
| SM secret type in console? | **Other type of secret** for PEM/passwords |

### Formulas (copy-paste)

```text
connections  = hubs × 1.5
secrets      = ceil(hubs/10) + hubs + connections + 3
sm_monthly   = secrets × 0.40
rds_monthly  ≈ 17 (micro) or 29 (small) + 2 storage
total        = sm_monthly + rds_monthly
```

---

## Related documents

| Document | Purpose |
|----------|---------|
| [RDS_POSTGRESQL_GUIDE.md](./RDS_POSTGRESQL_GUIDE.md) | Step-by-step RDS create |
| [RDS_CAPACITY_COST_PLANNING.md](./RDS_CAPACITY_COST_PLANNING.md) | Tiers up to 2,000 hubs |
| [DATABASE.md](./DATABASE.md) | What goes in RDS vs SM (T1–T6) |

---

## 12. 1:1 costing, three ratios & when to upgrade RDS

### 12.1 One hub, one connection — the **6 secrets** case (1:1)

When each hub has **exactly 1 project connection** (simplest model):

```text
 1   APS app secret       ceil(1÷10) = 1
 1   SSA PEM              1 ACC robot for this hub
 1   DBX SP secret        1 project connection (1:1)
 3   Platform secrets     DEK + Flask + platform key (shared)
───
 6   total secrets  ×  $0.40  =  ~$2.40/mo SM
```

| Line item | $/mo |
|-----------|------|
| Secrets Manager (6 × $0.40) | **~$2.40** |
| RDS `db.t4g.micro` | **~$17** |
| **Total** | **~$19/mo** |

Compare to **1 hub × 1.5 connections** (2 DBX secrets): `1+1+2+3 = 7` → **~$20/mo**.

---

### 12.2 Three connection ratios — same hubs, different SM cost

**Formula:**

```text
connections = hubs × ratio        # ratio = 1, 1.5, or 2
secrets     = ceil(hubs/10) + hubs + connections + 3
SM $/mo     = secrets × 0.40
```

| Ratio | Meaning | Example @ 10 hubs |
|-------|---------|-------------------|
| **1:1** | 1 project per hub | 10 connections |
| **1:1.5** | Budget default | 15 connections |
| **1:2** | High multi-project | 20 connections |

#### Full table — 1, 10, 50, 100 hubs (with RDS pick per row)

| Hubs | Users | Ratio | Conn | Secrets | SM $ | **RDS pick** | RDS $ | **Total** | **Best recommend?** |
|------|-------|-------|------|---------|------|--------------|-------|-----------|------------------------|
| 1 | 1 | **1:1** | 1 | 6 | $2.40 | **micro** | $17 | **~$19** | **Yes — cheapest start** |
| 1 | 1 | 1:1.5 | 2 | 7 | $2.80 | **micro** | $17 | ~$20 | Budget if 2 projects soon |
| 1 | 1 | 1:2 | 2 | 7 | $2.80 | **micro** | $17 | ~$20 | Only if 2 projects confirmed |
| 10 | 10 | **1:1** | 10 | 24 | $9.60 | **micro** | $17 | **~$27** | Floor cost pilot |
| 10 | 10 | **1:1.5** | 15 | 29 | $11.60 | **micro** | $17 | **~$29** | **Yes — best 10-hub pilot** |
| 10 | 10 | 1:2 | 20 | 34 | $13.60 | **micro** | $17 | ~$31 | Multi-project-heavy pilot |
| 50 | 50 | **1:1** | 50 | 108 | $43.20 | **micro** | $17 | **~$60** | Floor cost @ 50 hubs |
| 50 | 50 | **1:1.5** | 75 | 133 | $53.20 | **micro** | $17 | **~$69** | **Yes — best 50-hub pilot** |
| 50 | 50 | 1:2 | 100 | 158 | $63.20 | micro ⚠ | $17 | ~$80 | POC only — see §12.5 row 9 |
| 100 | 100 | **1:1** | 100 | 213 | $85.20 | **small** | $29 | **~$114** | Floor cost @ 100 hubs prod |
| 100 | 100 | **1:1.5** | 150 | 263 | $105.20 | **small** | $29 | **~$135** | **Yes — best 100-hub prod** |
| 100 | 100 | 1:2 | 200 | 313 | $125.20 | **small** | $29 | ~$154 | Heavy multi-project prod |

**Legend:** **micro** = `db.t4g.micro` (1 GB RAM, ~$17/mo) · **small** = `db.t4g.small` (2 GB RAM, ~$29/mo) · ⚠ = micro OK for POC; **upgrade to small for prod**

**Users column:** 1 hub admin per hub — users = hubs. Users do **not** drive RDS size; **connections + hub count** do.

#### Side-by-side — total $/mo only

```text
              1 hub    10 hubs   50 hubs   100 hubs
            ───────   ────────   ────────   ─────────
1:1  (min)    ~$19      ~$27       ~$60       ~$114
1:1.5 (budget) ~$20      ~$29       ~$69       ~$135
1:2  (high)    ~$20      ~$31       ~$80       ~$154
```

**Pattern:** Same RDS machine for 1–50 hubs (~$17). **SM grows with hubs + connections** — ratio choice matters more at scale than at 1 hub.

---

### 12.3 When to change RDS machine — hub **n** and users

**Important:** **Users do not pick the machine.** We assume **1 hub admin = 1 user**, so user count = hub count. What drives RDS size is **hub count**, **connection count**, and **app DB pool** — not login count.

#### Upgrade decision chart

```text
Hubs (n)     Users      Connections          RDS pick          Total ~$/mo (1:1.5)
─────────────────────────────────────────────────────────────────────────────────
1            1          1–2                  db.t4g.micro      ~$19–20
1–10         1–10       up to 15             db.t4g.micro      ~$20–29
11–50        11–50      up to 75             db.t4g.micro      ~$30–69
51–99        51–99      76–148 (1:1.5)       micro OR small*   ~$70–95
≥ 100        ≥ 100      ≥ 150 (1:1.5)        db.t4g.small      ~$135+
500+         500+       750+                 db.t4g.medium     ~$350+
```

\* **51–99 hubs @ 1:1.5:** connections exceed **75** → consider **small** early for prod; **micro OK for POC** if CPU stays low.

#### Simple rules (when **n** = hub count)

| Rule | Action |
|------|--------|
| **n ≤ 50 hubs** | Stay on **`db.t4g.micro`** (~$17/mo) |
| **n ≥ 100 hubs** | Move to **`db.t4g.small`** (~$29/mo) |
| **n = 51–99** @ **1:1** only | **micro OK** (≤99 connections) |
| **n = 51–99** @ **1:1.5** | **Upgrade to small** (connections > 75) |
| **n ≥ 500** | Plan **`db.t4g.medium`** (~$57/mo) |
| CloudWatch CPU **> 70%** sustained | Upgrade **regardless of n** |

#### Visual — hub count vs RDS instance

```text
RDS instance
│
│  medium ────────────────────────────────────────► 500+ hubs
│
│  small  ───────────────► from ~100 hubs (prod)
│              ╭─ optional early upgrade @ 51+ hubs if 1:1.5
│
│  micro  ████████████████████
│         1        10        50        100       hubs (n)
│         │                 │          │
│         │                 │          └── switch micro → small (prod)
│         │                 └── last stop on micro (POC/pilot)
│         └── 1 hub ~$19 (6 secrets, 1:1)
```

#### Users vs machine — do not confuse

| Question | Answer |
|----------|--------|
| After **how many users** change machine? | **Same as hubs** (1 user/hub) — but **users are not the driver** |
| What actually triggers upgrade? | **Hub count ≥ 100**, or **connections > ~75**, or **high CPU** |
| 100 users on 50 hubs? | Still **50 hubs** → stay **micro** if connections ≤ 75 |
| 50 users on 100 hubs? | **100 hubs** → use **small** even if only 50 logins |

---

### 12.4 Quick pick — which ratio to plan?

| Plan for… | Use ratio | 100 hubs total |
|-----------|-----------|----------------|
| **Best case / floor** | **1:1** | **~$114/mo** |
| **Production budget** | **1:1.5** | **~$135/mo** |
| **Heavy multi-project** | **1:2** | **~$154/mo** |

---

### 12.5 Every row — RDS pick, when to use, when NOT to use

Use this table to pick **ratio + RDS instance** for your hub/user count. **Bold rows** = our default recommendation.

| # | Hubs | Users | Ratio | Conn | Secrets | RDS | Total | **Use when** | **Do NOT use when** |
|---|------|-------|-------|------|---------|-----|-------|--------------|---------------------|
| **1** | 1 | 1 | **1:1** | 1 | 6 | **micro** | ~$19 | First POC; **1 ACC project only**; minimum spend | Hub will add 2+ projects soon (use row 2) |
| 2 | 1 | 1 | 1:1.5 | 2 | 7 | **micro** | ~$20 | 1 hub, **planning ~2 project links** | Only 1 project forever (row 1 saves $0.40/mo) |
| 3 | 1 | 1 | 1:2 | 2 | 7 | **micro** | ~$20 | Policy = **2 projects per hub** from day 1 | Same cost as 1:1.5 at 1 hub — pick one policy |
| **4** | 10 | 10 | **1:1** | 10 | 24 | **micro** | ~$27 | Pilot where **every customer = 1 project** | Typical SaaS (most hubs have 2+ projects) |
| **5** | 10 | 10 | **1:1.5** | 15 | 29 | **micro** | ~$29 | **Default 10-hub pilot** — realistic mix | All customers single-project (row 4 cheaper) |
| 6 | 10 | 10 | 1:2 | 20 | 34 | **micro** | ~$31 | Pilot with **multi-project-heavy** customers | Tight budget — row 5 saves ~$2/mo |
| **7** | 50 | 50 | **1:1** | 50 | 108 | **micro** | ~$60 | **50 hubs, 1 project each** — SM floor | Approaching prod SLA / Multi-AZ (plan small) |
| **8** | 50 | 50 | **1:1.5** | 75 | 133 | **micro** | ~$69 | **Default 50-hub pilot** before prod | CPU > 70% or connections growing past 75 |
| 9 | 50 | 50 | 1:2 | 100 | 158 | **micro*** | ~$80 | POC only; **100 sync jobs** on micro | **Prod** — use **small** (+$12/mo) for headroom |
| **10** | 100 | 100 | **1:1** | 100 | 213 | **small** | ~$114 | **100 hubs prod**, 1 project/hub — cost floor | **Never stay on micro** at 100 hubs |
| **11** | 100 | 100 | **1:1.5** | 150 | 263 | **small** | ~$135 | **Default 100-hub production** | All hubs 1-project (row 10 saves ~$21/mo) |
| 12 | 100 | 100 | 1:2 | 200 | 313 | **small** | ~$154 | Prod with **many projects per hub** | Budget cap — row 11 saves ~$19/mo |

\* Row 9: **micro works for data size** (DB still ~40 MB), but **100 connections** is the upper comfort limit. For prod, switch to **small** — adds **~$12/mo** RDS, total ~**$92/mo** instead of ~$80.

---

### 12.6 Why micro vs small — one rule per scale

| Hubs | Users | When **micro** ($17) | When **small** ($29) | Why |
|------|-------|----------------------|----------------------|-----|
| **1–50** | 1–50 | **Always** (all ratios) | Optional row 9 only if prod + 1:2 | Data < 50 MB; ≤75 conn @ 1:1.5 fits 1 GB RAM |
| **51–99** | 51–99 | **1:1 only** (≤99 conn) | **1:1.5 or 1:2** (conn > 75) | micro max ~80–100 DB connections comfortably |
| **≥ 100** | ≥ 100 | **Never for prod** | **Always** (all ratios) | Prod budget tier; 2 GB RAM; ~150+ connections @ 1:1.5 |

```text
                    micro OK              upgrade to small
                    ─────────────────     ───────────────────►
1 hub ──── 10 hubs ──── 50 hubs ──── 51–99* ──── 100+ hubs
  ~$19       ~$29         ~$69        ~$70–95      ~$114–154
                                  * 1:1.5 @ 51+ → small recommended

Cost of upgrading micro → small: +~$12/mo RDS (fixed until next tier)
```

#### Default picks (copy for PM slide)

| Stage | Hubs | Users | Ratio | RDS | Total | Why |
|-------|------|-------|-------|-----|-------|-----|
| **First POC** | 1 | 1 | 1:1 | micro | **~$19** | 6 secrets; 1 project; cheapest |
| **Pilot** | 10 | 10 | 1:1.5 | micro | **~$29** | Realistic projects/hub; still one micro |
| **Pre-prod** | 50 | 50 | 1:1.5 | micro | **~$69** | Last tier on micro before upgrade |
| **Production** | 100 | 100 | 1:1.5 | **small** | **~$135** | SM dominates; small RDS for SLA |

---

*Document version: 1.2 — Aug 2026. Added per-row RDS guidance (§12.5–12.6).*
