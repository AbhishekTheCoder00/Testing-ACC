# Staging-Only AWS Hosting Guide — ACC Connector

> **Audience:** You are hosting **one** Staging environment on AWS for the ACC Connector POC. Dev and Production are **out of scope for now**.
>
> **Your Staging environment:**
>
> | Item | Value |
> |------|-------|
> | Hostname | `formabricks-stg.cctech.co.in` |
> | Elastic IP | `32.197.105.164` |
> | Deploy branch | `staging-deployment` |
>
> **Related docs:**
> - [.env.example](.env.example) — required environment variables
> - [deploy/acc-connector.service](deploy/acc-connector.service) — systemd unit for Gunicorn
> - [deploy/nginx-acc-connector.conf](deploy/nginx-acc-connector.conf) — Nginx site template
> - [`.github/workflows/acc-connector-ci.yml`](../.github/workflows/acc-connector-ci.yml) — CI/CD workflow

---

## Table of Contents

1. [Scope and current state](#1-scope-and-current-state)
2. [`.env` vs `connector.db` — multi-user on one server](#2-env-vs-connectordb--multi-user-on-one-server)
3. [Staging domain (already registered)](#3-staging-domain-already-registered)
4. [AWS setup — one t3.small instance (click-by-click)](#4-aws-setup--one-t3small-instance-click-by-click)
5. [OAuth registration — Staging URLs only](#5-oauth-registration--staging-urls-only)
6. [One-time Ubuntu server setup](#6-one-time-ubuntu-server-setup)
7. [Enable CI/CD deploy](#7-enable-cicd-deploy)
8. [First deploy and verification](#8-first-deploy-and-verification)
9. [Cost estimate](#9-cost-estimate)
10. [When you add Dev / Production later](#10-when-you-add-dev--production-later)

---

## 1. Scope and current state

### In scope

| Item | Value |
|------|-------|
| EC2 instances | **1** (Staging only) |
| Instance type | **t3.small** (2 vCPU, 2 GB RAM) |
| OS | Ubuntu 22.04 LTS |
| Storage | 30 GB gp3 EBS |
| Stack | Nginx (HTTPS) → Gunicorn → Flask → SQLite |
| Deploy branch | `staging-deployment` |
| Staging hostname | `formabricks-stg.cctech.co.in` |
| Elastic IP | `32.197.105.164` |

### Out of scope (for now)

- Dev EC2, Production EC2
- Separate OAuth apps for Dev/Prod
- `DEV_*` / `PROD_*` GitHub secrets

### Current CI state

The workflow at [`.github/workflows/acc-connector-ci.yml`](../.github/workflows/acc-connector-ci.yml) runs **smoke tests** on every push/PR to `staging-deployment`:

- `python -m py_compile` on key backend files (refactored paths under `backend/services/`, `backend/clients/`, `backend/repositories/`)
- `python scripts/smoke_check.py` (with a dummy `SECRET_KEY` in CI only)

**Deploy is not running yet** until you set `ENABLE_DEPLOY=true` and configure GitHub secrets (see [Section 7](#7-enable-cicd-deploy)).

```mermaid
flowchart LR
  PR[PR or push staging-deployment] --> Test[Smoke tests]
  Test -->|ENABLE_DEPLOY plus secrets| Deploy[Deploy Staging EC2]
  Deploy --> Nginx[Nginx HTTPS]
  Nginx --> Gunicorn[Gunicorn]
  Gunicorn --> Flask[Flask]
  Flask --> DB[(connector.db)]
```

---

## 2. `.env` vs `connector.db` — multi-user on one server

**Testers do not each get a `.env` file.** There is one server config and one shared database file with per-user rows.

### Two layers

| Layer | What it holds | Who shares it | Where it lives |
|-------|---------------|---------------|----------------|
| **Server `.env`** | `SECRET_KEY`, APS OAuth app credentials, Databricks OAuth app credentials, redirect URIs | **Everyone** on this Staging server | `/opt/acc-connector/app/.env` |
| **`connector.db`** | Encrypted ACC/Databricks tokens, hub/project selection, bootstrap state, sync history | **One partition per APS user** (`user_id`) | `/var/lib/acc-connector/connector.db` (symlinked into app dir) |

### What happens when a tester signs in

1. Tester opens `https://formabricks-stg.cctech.co.in`.
2. Flask sets a session cookie signed with the server `SECRET_KEY`.
3. **Sign in with Autodesk** → APS returns a `user_id` → stored in the Flask session → ACC tokens saved in `connector.db` under that `user_id`.
4. **Sign in with Databricks** → tokens saved under the same `user_id`.
5. Bootstrap and sync state are isolated per `user_id`. Two testers on the same Staging server do not overwrite each other.

### Rules

| Rule | Why |
|------|-----|
| Create **one** Staging `.env` from [.env.example](.env.example) | OAuth apps are per-environment, not per-user |
| Use **HTTPS** redirect URIs in `.env` | OAuth providers reject `http://` callbacks on public hosts |
| **Never** commit `.env` to git | Contains secrets |
| **Never** copy Production secrets into Staging | Separate environments, separate OAuth apps |
| **Do not rotate `SECRET_KEY` casually** | It derives the Fernet encryption key in [backend/repositories/state_store.py](backend/repositories/state_store.py); changing it invalidates all encrypted tokens in `connector.db` |
| CI deploy **never overwrites** `.env` or `connector.db` | `rsync --exclude '.env' --exclude 'connector.db'` in the workflow |

### Optional: AWS Secrets Manager

Store Staging secrets in AWS Secrets Manager as `acc-connector/staging` (JSON or key-value). On first server setup, copy values into `/opt/acc-connector/app/.env` manually. The connector reads `.env` at runtime — it does not pull from Secrets Manager automatically.

Generate a `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Example Staging `.env`:

```ini
SECRET_KEY=<unique-32-byte-hex>
APS_CLIENT_ID=<staging-aps-client-id>
APS_CLIENT_SECRET=<staging-aps-secret>
APS_REDIRECT_URI=https://formabricks-stg.cctech.co.in/callback
DATABRICKS_CLIENT_ID=<staging-dbx-oauth-client-id>
DATABRICKS_CLIENT_SECRET=<staging-dbx-oauth-client-secret>
DATABRICKS_REDIRECT_URI=https://formabricks-stg.cctech.co.in/databricks/callback
```

```bash
sudo chown accconnector:accconnector /opt/acc-connector/app/.env
sudo chmod 600 /opt/acc-connector/app/.env
```

---

## 3. Staging domain (already registered)

Your DNS is configured:

| Record | Value |
|--------|-------|
| Type | A |
| Hostname | `formabricks-stg.cctech.co.in` |
| IP Address | `32.197.105.164` |

Verify propagation:

```bash
nslookup formabricks-stg.cctech.co.in
# or: dig formabricks-stg.cctech.co.in +short
# Expected: 32.197.105.164
```

### Hostname checklist — must match exactly

Use the **same** hostname in every row. `https` only, **no trailing slash**.

| Where | Value |
|-------|-------|
| DNS A record | `formabricks-stg.cctech.co.in` → `32.197.105.164` |
| Nginx `server_name` | `formabricks-stg.cctech.co.in` |
| Certbot `-d` flag | `formabricks-stg.cctech.co.in` |
| `.env` → `APS_REDIRECT_URI` | `https://formabricks-stg.cctech.co.in/callback` |
| `.env` → `DATABRICKS_REDIRECT_URI` | `https://formabricks-stg.cctech.co.in/databricks/callback` |
| APS app callback URL | `https://formabricks-stg.cctech.co.in/callback` |
| Databricks OAuth app redirect URI | `https://formabricks-stg.cctech.co.in/databricks/callback` |
| GitHub secret `STAGING_PUBLIC_HOST` | `formabricks-stg.cctech.co.in` |
| GitHub secret `STAGING_HOST` | `32.197.105.164` |

---

## 4. AWS setup — one t3.small instance (click-by-click)

Follow these steps in order. Total time: about 20–30 minutes.

### What you will create

| AWS object | Name (example) | Purpose |
|------------|----------------|---------|
| Key pair | `acc-connector-deploy` | `.pem` file to SSH into the server |
| Security group | `acc-connector-sg` | Firewall: SSH + HTTP + HTTPS |
| EC2 instance | `acc-connector-staging` | Your Ubuntu Linux Staging server |
| Elastic IP | `32.197.105.164` | Fixed public IP for DNS and GitHub secrets |

### Summary table (reference)

| Setting | Value |
|---------|-------|
| AMI | Ubuntu 22.04 LTS |
| Instance type | **t3.small** (2 vCPU, 2 GB RAM) |
| Storage | 30 GB gp3 EBS (root volume) |
| Count | **1** |
| Key pair | `acc-connector-deploy` — save `.pem` securely |

| Port | Source | Purpose |
|------|--------|---------|
| 22 | Your office IP / VPN CIDR | SSH (avoid `0.0.0.0/0` if possible) |
| 80 | `0.0.0.0/0` | HTTP — Certbot + redirect to HTTPS |
| 443 | `0.0.0.0/0` | HTTPS app traffic |

---

### Step 4.1 — Log into AWS Console

1. Open your browser → go to [https://aws.amazon.com](https://aws.amazon.com)
2. Click **Sign In to the Console** (top right)
3. Sign in with your AWS account
4. At the top of the page, check the **region** dropdown (e.g. **US East (N. Virginia) `us-east-1`**)
   - Pick a region close to you and **stay in that region** for all steps below
   - EC2, Elastic IP, and DNS must all use the **same region**

---

### Step 4.2 — Create the SSH key pair (do this BEFORE launching EC2)

You need a `.pem` file to connect to the server. AWS only lets you download it **once**.

1. In the AWS search bar at the top, type **EC2** → click **EC2**
2. In the left sidebar, scroll to **Network & Security** → click **Key Pairs**
3. Click the orange **Create key pair** button (top right)
4. Fill in the form:

| Field | What to enter |
|-------|----------------|
| **Name** | `acc-connector-deploy` |
| **Key pair type** | **RSA** |
| **Private key file format** | **.pem** (for Mac/Linux/Windows OpenSSH) |

5. Click **Create key pair**
6. Your browser downloads **`acc-connector-deploy.pem`**
7. **Move the file somewhere safe**, e.g. `C:\Users\YourName\.ssh\acc-connector-deploy.pem`
8. **Never commit this file to GitHub** — paste contents into GitHub secret `SSH_PRIVATE_KEY` later ([Section 7](#7-enable-cicd-deploy))

> **If you lose the `.pem` file**, you cannot SSH into that instance. Create a new key pair and launch a new instance.

---

### Step 4.3 — Launch the EC2 instance

1. In the EC2 left sidebar, click **Instances**
2. Click the orange **Launch instances** button
3. Fill each section of the form as below.

#### Section: Name and tags

| Field | Value |
|-------|-------|
| **Name** | `acc-connector-staging` |

#### Section: Application and OS Images (AMI)

1. Click **Ubuntu** (or search "Ubuntu")
2. Select **Ubuntu Server 22.04 LTS (HVM), SSD Volume Type**
3. Architecture: **64-bit (x86)** — default

#### Section: Instance type

1. Click the instance type dropdown
2. In the search box, type `t3.small`
3. Select **t3.small** (2 vCPU, 2 GiB Memory)
4. **Do not** pick t2.micro — Staging needs more RAM for Gunicorn + sync runs

#### Section: Key pair (login)

1. Click the **Key pair** dropdown
2. Select **acc-connector-deploy** (created in Step 4.2)
3. Do **not** select "Proceed without a key pair"

#### Section: Network settings

Click **Edit** on the right side of this section.

| Setting | What to choose |
|---------|----------------|
| **VPC** | Leave default |
| **Subnet** | Leave default |
| **Auto-assign public IP** | **Enable** |
| **Firewall (security groups)** | Select **Create security group** |

In the **Security group name** field, enter: `acc-connector-sg`

Under **Inbound Security Group Rules**, add **three rules**:

**Rule 1 — SSH (admin access):**

| Column | Value |
|--------|-------|
| Type | **SSH** |
| Port | 22 (auto-filled) |
| Source type | **My IP** (recommended) or **Custom** with your office/VPN CIDR |
| Source | Auto-fills your current public IP, or e.g. `203.0.113.0/24` |

> **My IP** limits SSH to your current computer. For GitHub Actions deploy, GitHub uses changing IPs — you may need to widen SSH to `0.0.0.0/0` temporarily for POC. Avoid leaving SSH open to the world long-term.

**Rule 2 — HTTP (Certbot + redirect to HTTPS):**

Click **Add security group rule**

| Column | Value |
|--------|-------|
| Type | **HTTP** |
| Port | 80 |
| Source type | **Anywhere** |
| Source | `0.0.0.0/0` |

**Rule 3 — HTTPS (app traffic):**

Click **Add security group rule**

| Column | Value |
|--------|-------|
| Type | **HTTPS** |
| Port | 443 |
| Source type | **Anywhere** |
| Source | `0.0.0.0/0` |

#### Section: Configure storage

Click **Advanced** if the storage section is collapsed.

| Setting | Value |
|---------|-------|
| **Size** | **30** GiB |
| **Volume type** | **gp3** |
| **Delete on termination** | Leave checked (default) |

#### Section: Advanced details

Leave everything default. **Do not** change anything here for Staging.

#### Launch

1. On the right **Summary** panel, confirm **Number of instances** = **1**
2. Review the summary (t3.small, Ubuntu 22.04, `acc-connector-deploy` key)
3. Click the orange **Launch instance** button
4. You should see "Successfully initiated launch of instance"
5. Click **View all instances**

#### Wait until the instance is ready

1. On the **Instances** page, find `acc-connector-staging`
2. Wait until **Instance state** = **Running** (refresh every 10–20 seconds)
3. Wait until **Status check** = **2/2 checks passed** (may take 1–2 minutes)
4. Click the instance row → in the bottom **Details** tab, note **Public IPv4 address**

---

### Step 4.4 — Allocate and associate Elastic IP

A normal EC2 public IP changes when you stop/start the instance. An **Elastic IP** stays the same — required for DNS A records and GitHub secret `STAGING_HOST`.

#### Step 4.4a — Allocate an Elastic IP

1. AWS Console → **EC2**
2. Left sidebar → **Network & Security** → **Elastic IPs**
3. Click **Allocate Elastic IP address** (orange button)
4. Click **Allocate**
5. Confirm the Elastic IP is **`32.197.105.164`** (or note the allocated IP if different)

#### Step 4.4b — Associate Elastic IP with your instance

1. On the **Elastic IPs** page, select the row for `32.197.105.164`
2. Click **Actions** → **Associate Elastic IP address**
3. Select instance **acc-connector-staging**
4. Click **Associate**
5. Go to **Instances** → confirm **Public IPv4 address** = `32.197.105.164`

#### How GitHub Actions uses this IP

| GitHub secret | Value |
|---------------|-------|
| `STAGING_HOST` | `32.197.105.164` — SSH/rsync target |
| `STAGING_PUBLIC_HOST` | `formabricks-stg.cctech.co.in` — health check URL |

```
Elastic IP 32.197.105.164
        ↓
DNS A record formabricks-stg.cctech.co.in → 32.197.105.164
        ↓
Paste IP into GitHub secret STAGING_HOST
        ↓
deploy job: ssh ubuntu@32.197.105.164
        ↓
Code lands on /opt/acc-connector/app/
```

> **Warning:** An unattached Elastic IP incurs a small monthly charge. Always attach it to the running instance. **Release** the Elastic IP if you terminate the server.

---

### Step 4.5 — Checklist before server setup

- [ ] Instance state = **Running**
- [ ] Status check = **2/2 checks passed**
- [ ] You have `acc-connector-deploy.pem` saved on your PC
- [ ] Security group `acc-connector-sg` allows ports **22**, **80**, **443**
- [ ] Elastic IP `32.197.105.164` associated with `acc-connector-staging`
- [ ] DNS A record `formabricks-stg.cctech.co.in` → `32.197.105.164`

**Quick SSH test** (from your PC):

```powershell
ssh -i C:\Users\YourName\.ssh\acc-connector-deploy.pem ubuntu@32.197.105.164
```

Type `yes` on first connect. You should see `ubuntu@ip-172-31-xx-xx:~$`. Type `exit` to disconnect.

Next: [Section 5](#5-oauth-registration--staging-urls-only) (OAuth) and [Section 6](#6-one-time-ubuntu-server-setup) (server commands).

---

## 5. OAuth registration — Staging URLs only

Register callbacks **only** for your Staging hostname. Do not register Dev or Production URLs yet.

### Autodesk Platform Services (APS)

At [https://aps.autodesk.com/myapps/](https://aps.autodesk.com/myapps/):

| Setting | Value |
|---------|-------|
| Callback URL | `https://formabricks-stg.cctech.co.in/callback` |

Copy `APS_CLIENT_ID` and `APS_CLIENT_SECRET` into Staging `.env`.

### Databricks OAuth app

In your **Staging** Databricks workspace: Settings → Developer → OAuth Applications.

| Setting | Value |
|---------|-------|
| Redirect URI | `https://formabricks-stg.cctech.co.in/databricks/callback` |

Copy `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` into Staging `.env`.

---

## 6. One-time Ubuntu server setup

SSH in:

```bash
ssh -i acc-connector-deploy.pem ubuntu@32.197.105.164
```

### 6.1 System packages and Python

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git rsync

python3.11 --version
```

### 6.2 App user and directories

```bash
sudo useradd --system --home /opt/acc-connector --shell /usr/sbin/nologin accconnector || true
sudo mkdir -p /opt/acc-connector /var/lib/acc-connector
sudo chown -R accconnector:accconnector /opt/acc-connector /var/lib/acc-connector
```

First-time code on server (CI `rsync` replaces this after deploy is enabled):

```bash
sudo -u accconnector git clone https://github.com/cctech-labs/FormaDatabricksIntegration.git /opt/acc-connector/repo
sudo -u accconnector ln -sfn /opt/acc-connector/repo/acc-connector /opt/acc-connector/app
```

### 6.3 Python virtual environment

```bash
cd /opt/acc-connector/app
sudo -u accconnector python3.11 -m venv /opt/acc-connector/venv
sudo -u accconnector /opt/acc-connector/venv/bin/pip install --upgrade pip
sudo -u accconnector /opt/acc-connector/venv/bin/pip install -r requirements.txt
```

### 6.4 Server `.env`

Create `/opt/acc-connector/app/.env` per [Section 2](#2-env-vs-connectordb--multi-user-on-one-server).

### 6.5 Persistent `connector.db`

`connector.db` stores encrypted tokens and per-user state. It must survive code deploys.

```bash
sudo -u accconnector touch /var/lib/acc-connector/connector.db
sudo rm -f /opt/acc-connector/app/connector.db
sudo -u accconnector ln -s /var/lib/acc-connector/connector.db /opt/acc-connector/app/connector.db
sudo chown accconnector:accconnector /var/lib/acc-connector/connector.db
```

Why: GitHub Actions `rsync` updates `/opt/acc-connector/app/` but excludes `connector.db`. The real file lives in `/var/lib/acc-connector/` so deploys never wipe tester tokens or bootstrap state.

### 6.6 systemd service (Gunicorn)

```bash
sudo cp /opt/acc-connector/app/deploy/acc-connector.service /etc/systemd/system/acc-connector.service
sudo systemctl daemon-reload
sudo systemctl enable acc-connector
sudo systemctl start acc-connector
sudo systemctl status acc-connector
```

Verify:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/
# Expect 200
```

The unit uses `--workers 2` and `--timeout 600` — appropriate for t3.small and long sync runs.

### 6.7 Nginx + HTTPS

Copy the Nginx config and set `server_name` to `formabricks-stg.cctech.co.in`:

```bash
sudo cp /opt/acc-connector/app/deploy/nginx-acc-connector.conf /etc/nginx/sites-available/acc-connector
sudo sed -i 's/YOUR_DOMAIN/formabricks-stg.cctech.co.in/g' /etc/nginx/sites-available/acc-connector
sudo ln -s /etc/nginx/sites-available/acc-connector /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

sudo certbot --nginx -d formabricks-stg.cctech.co.in
```

Optional — password-protect Staging (uncomment `auth_basic` lines in the Nginx config):

```bash
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd qa_tester
```

### 6.8 Firewall (optional)

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

### 6.9 GitHub Actions SSH access

Add the deploy public key to the server:

```bash
# On EC2, as ubuntu user:
echo "<github-actions-public-key>" >> ~/.ssh/authorized_keys
```

Or paste the contents of `acc-connector-deploy.pem` into GitHub secret `SSH_PRIVATE_KEY` and use the same key for manual SSH and CI deploy.

---

## 7. Enable CI/CD deploy

The workflow already deploys Staging on push to `staging-deployment` when deploy is enabled. No workflow branch edit is required.

### Step 1 — GitHub repository variable

GitHub → Settings → Secrets and variables → Actions → **Variables**:

| Name | Value |
|------|-------|
| `ENABLE_DEPLOY` | `true` |

### Step 2 — GitHub secrets (Staging only)

GitHub → Settings → Secrets and variables → Actions → **Secrets**:

| Secret | Your value | Used for |
|--------|------------|----------|
| `SSH_PRIVATE_KEY` | Contents of `acc-connector-deploy.pem` | SSH/rsync to EC2 |
| `DEPLOY_USER` | `ubuntu` | SSH user on EC2 |
| `STAGING_HOST` | `32.197.105.164` | SSH target |
| `STAGING_PUBLIC_HOST` | `formabricks-stg.cctech.co.in` | Post-deploy `curl /health` |

**Not needed now:** `DEV_HOST`, `DEV_PUBLIC_HOST`, `PROD_HOST`, `PROD_PUBLIC_HOST`.

### Step 3 — GitHub Environment

Create environment **`staging`** (Settings → Environments). No required reviewers needed for a POC.

### What deploy does

On push to `staging-deployment` (after tests pass):

```bash
rsync -avz --delete \
  --exclude '.env' \
  --exclude 'connector.db' \
  ...
  ./ deploy-user@32.197.105.164:/opt/acc-connector/app/

ssh ... 'pip install -r requirements.txt && sudo systemctl restart acc-connector'
curl -sf https://formabricks-stg.cctech.co.in/health
```

`.env` and `connector.db` on the server are never touched by deploy.

### What happens on each push

```
You push to staging-deployment
       ↓
GitHub Actions: py_compile + smoke_check
       ↓
If tests PASS and ENABLE_DEPLOY=true
       ↓
rsync acc-connector/ → /opt/acc-connector/app/
       ↓
pip install + restart Gunicorn
       ↓
curl https://formabricks-stg.cctech.co.in/health
```

| On server | Updated by deploy? |
|-----------|---------------------|
| Python code (`app.py`, `backend/`, etc.) | **Yes** |
| `requirements.txt` → pip install | **Yes** |
| `.env` (secrets) | **No** |
| `connector.db` (logins, tokens) | **No** |
| Nginx config | **No** |
| SSL cert | **No** — certbot handles renewal |

---

## 8. First deploy and verification

### Ordered checklist

- [x] DNS A record `formabricks-stg.cctech.co.in` → `32.197.105.164`
- [ ] EC2 t3.small running, security group open on 80/443
- [ ] Server setup complete (Section 6): venv, `.env`, `connector.db` symlink, systemd, Nginx, Certbot
- [ ] APS and Databricks OAuth apps registered with Staging URLs
- [ ] `ENABLE_DEPLOY=true` and Staging secrets configured
- [ ] Push to `staging-deployment` → Actions green for test + deploy-staging

### Quick verification

| Check | Command / action | Expected |
|-------|------------------|----------|
| HTTPS | `curl -I https://formabricks-stg.cctech.co.in/` | `HTTP/2 200` |
| Health | `curl -sf https://formabricks-stg.cctech.co.in/health` | `{"status":"ok"}` |
| Gunicorn | `sudo systemctl status acc-connector` | `active (running)` |
| ACC OAuth | Browser → Sign in Autodesk | Redirect to `/callback` without error |
| Databricks OAuth | Sign in Databricks | Redirect to `/databricks/callback` |
| Multi-user | Two testers sign in with different ACC accounts | Each sees own hub/project state |

### Troubleshooting

**View logs:**

```bash
sudo journalctl -u acc-connector -n 100 --no-pager
sudo tail -f /var/log/nginx/error.log
```

Common issues:

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| 502 Bad Gateway | Gunicorn not running | `sudo systemctl restart acc-connector` |
| OAuth redirect mismatch | URL typo in `.env` or provider console | Match [hostname checklist](#hostname-checklist--must-match-exactly) exactly |
| Deploy SSH fails | Security group blocks port 22 from GitHub IPs | Temporarily allow `0.0.0.0/0` on port 22 for POC |
| SQLite permission error | Wrong owner on `connector.db` | `sudo chown accconnector:accconnector /var/lib/acc-connector/connector.db` |

If something goes wrong after deploy:

```bash
sudo systemctl restart acc-connector
sudo journalctl -u acc-connector -n 50
```

---

## 9. Cost estimate

Approximate USD/month for **us-east-1** (June 2026). Enterprise discounts and region vary.

| Item | Approx. cost |
|------|--------------|
| 1× EC2 `t3.small` on-demand | ~$15/month |
| 30 GB gp3 EBS | ~$2.40/month |
| 1 Elastic IP (attached) | Free |
| Route 53 hosted zone (if new domain) | ~$0.50/month |
| 1 Secrets Manager secret | ~$0.40/month |
| **Total** | **~$18–25/month** |

Databricks compute (warehouse, pipelines) is billed separately and is often larger than EC2.

**Save money:** Stop the EC2 instance nights/weekends if testers are not using Staging (~65% savings on compute).

---

## 10. When you add Dev / Production later

| Step | Action |
|------|--------|
| Infrastructure | Launch additional EC2 instances (or reuse the pattern from this guide) |
| Branches | Use `develop` → Dev, `staging` → Staging, `main` → Production |
| Secrets | Add `DEV_*` and `PROD_*` GitHub secrets; separate `.env` per server |
| OAuth | Register separate callback URLs per environment |
| Database | Separate `connector.db` per environment — never copy Production DB to Staging |

---

## Quick Start Checklist

**Today (CI already running):**

- [ ] Smoke tests pass on `staging-deployment` (after py_compile path fix)
- [x] Staging hostname registered: `formabricks-stg.cctech.co.in`
- [x] Elastic IP allocated: `32.197.105.164`
- [ ] Register APS + Databricks OAuth callbacks for Staging URL
- [ ] Launch 1× t3.small EC2 + associate Elastic IP (if not done)

**When AWS server is ready:**

- [ ] Complete [Section 6](#6-one-time-ubuntu-server-setup)
- [ ] Configure GitHub secrets ([Section 7](#7-enable-cicd-deploy))
- [ ] Push → deploy → verify OAuth and bootstrap
- [ ] Share Staging URL with QA: `https://formabricks-stg.cctech.co.in`

---

*Guide version: June 2026 — Staging-only, t3.small, branch `staging-deployment`, hostname `formabricks-stg.cctech.co.in`.*
