# AWS Free Tier Demo — Hosting Smoke Test

> **Purpose:** Validate **EC2 + Elastic IP + Nginx + Gunicorn + GitHub Actions deploy** on the **smallest free-tier instance** *before* you run the full [STAGING_HOSTING_GUIDE.md](STAGING_HOSTING_GUIDE.md) ACC Connector setup.
>
> **Demo app:** [demo/](demo/) — tiny Flask site that shows server `.env` values (secrets masked) and live state in `connector.db` (visit counter).
>
> **Related:**
> - [STAGING_HOSTING_GUIDE.md](STAGING_HOSTING_GUIDE.md) — real ACC Connector on t3.small (after demo passes)
> - [demo/demo-hosting-ci.yml](demo/demo-hosting-ci.yml) — **standalone workflow** for your separate demo GitHub repo
> - [demo/.env.example](demo/.env.example) — demo environment keys

---

## Table of Contents

1. [What this demo proves](#1-what-this-demo-proves)
2. [Demo app — `.env` vs `connector.db`](#2-demo-app--env-vs-connectordb)
3. [AWS setup — click-by-click (beginners)](#3-aws-setup--click-by-click-beginners)
4. [Elastic IP — click-by-click](#4-elastic-ip--click-by-click)
5. [SSH into your server (Windows)](#5-ssh-into-your-server-windows)
6. [Domain registration (optional)](#6-domain-registration-optional)
7. [One-time EC2 server setup (commands)](#7-one-time-ec2-server-setup-commands)
8. [GitHub — click-by-click](#8-github--click-by-click)
9. [Verify the demo](#9-verify-the-demo)
10. [Graduate to Staging](#10-graduate-to-staging)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What this demo proves

| Step | You learn |
|------|-----------|
| Launch free-tier EC2 | Ubuntu, security group, SSH |
| Elastic IP | Stable public IP for SSH and browser |
| Nginx + Gunicorn | Same pattern as real connector |
| `.env` on server | Never in git; excluded from `rsync` |
| `connector.db` on disk | Survives code deploy when stored outside app folder |
| GitHub Actions | `DEMO_HOST` secret = Elastic IP; deploy + `/health` curl |

```mermaid
flowchart LR
  subgraph github [Your demo GitHub repo]
    Push[push main] --> Test[test job]
    Test --> Deploy[deploy job]
  end
  subgraph aws [AWS Free Tier]
    EIP[Elastic IP]
    EC2[t2.micro or t3.micro]
    EIP --> EC2
  end
  Deploy -->|SSH rsync DEMO_HOST| EC2
  Browser[Browser] --> Nginx[Nginx :80]
  Nginx --> Gunicorn[Gunicorn :8001]
  Gunicorn --> Flask[app.py]
  Flask --> EnvFile[.env]
  Flask --> DB[(connector.db)]
```

**Order of operations:** Complete this demo → then follow [STAGING_HOSTING_GUIDE.md](STAGING_HOSTING_GUIDE.md) for the real ACC Connector.

---

## 2. Demo app — `.env` vs `connector.db`

Same mental model as the real connector — smaller surface area.

### Server `.env` (one file per demo EC2)

Copy [demo/.env.example](demo/.env.example) to `/opt/acc-demo/app/.env`:

| Key | Demo value | Shown on website? |
|-----|------------|-------------------|
| `SECRET_KEY` | `demo-secret-key-replace-with-token-hex-32` (or generate real hex) | Masked (`***…last4`) |
| `APP_NAME` | `ACC Connector Hosting Demo` | Yes |
| `SITE_ENV` | `demo-free-tier` | Yes |
| `DEPLOY_LABEL` | `v0.1-demo` | Yes — bump after each deploy to confirm CI ran |

Generate a better `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### `connector.db` (SQLite on disk)

| Table | Purpose |
|-------|---------|
| `site_state` | `visit_count`, `last_visit_at`, `note` — increments on each homepage load |

**Test persistence:**

1. Open homepage → `visit_count` = 1, 2, 3…
2. Push a code change → GitHub deploy runs → refresh page → count **stays** (DB not wiped).

### Run locally (optional)

```powershell
cd acc-connector/demo
copy .env.example .env
pip install -r requirements.txt
python app.py
# http://localhost:8001
```

---

## 3. AWS setup — click-by-click (beginners)

This section assumes you have **never used AWS before**. Follow the steps in order. Total time: about 20–30 minutes.

### What you will create

| AWS object | Name (example) | Purpose |
|------------|----------------|---------|
| Key pair | `acc-demo-deploy` | `.pem` file to SSH into the server |
| Security group | `acc-demo-sg` | Firewall: allow SSH + HTTP |
| EC2 instance | `acc-demo-server` | Your Ubuntu Linux server |
| Elastic IP | (auto-assigned) | Fixed public IP address |

### Instance choice (free tier)

| Type | vCPU | RAM | Free tier |
|------|------|-----|-----------|
| **t2.micro** | 1 | 1 GB | 750 hours/month for 12 months — **pick this** |
| t3.micro | 2 | 1 GB | Same free tier if t2.micro is unavailable in your region |

---

### Step 3.1 — Log into AWS Console

1. Open your browser → go to [https://aws.amazon.com](https://aws.amazon.com)
2. Click **Sign In to the Console** (top right)
3. Sign in with your **free tier / root / IAM** account
4. At the top of the page, check the **region** dropdown (e.g. **US East (N. Virginia) `us-east-1`**)
   - Pick a region close to you and **stay in that region** for all steps below
   - All resources (EC2, Elastic IP) must be in the **same region**

---

### Step 3.2 — Create the SSH key pair (do this BEFORE launching EC2)

You need a `.pem` file to connect to the server. AWS only lets you download it **once**.

1. In the AWS search bar at the top, type **EC2** → click **EC2**
2. In the left sidebar, scroll to **Network & Security** → click **Key Pairs**
3. Click the orange **Create key pair** button (top right)
4. Fill in the form:

| Field | What to enter |
|-------|----------------|
| **Name** | `acc-demo-deploy` |
| **Key pair type** | **RSA** |
| **Private key file format** | **.pem** (for Mac/Linux/Windows OpenSSH) |

5. Click **Create key pair**
6. Your browser downloads **`acc-demo-deploy.pem`**
7. **Move the file somewhere safe**, e.g. `C:\Users\YourName\.ssh\acc-demo-deploy.pem`
8. **Never commit this file to GitHub** — it is a private key

> **If you lose the `.pem` file**, you cannot SSH into that instance. You would need to create a new key pair and launch a new instance.

---

### Step 3.3 — Launch the EC2 instance

1. In the EC2 left sidebar, click **Instances**
2. Click the orange **Launch instances** button
3. You will see a multi-section form. Fill each section as below.

#### Section: Name and tags

| Field | Value |
|-------|-------|
| **Name** | `acc-demo-server` |

#### Section: Application and OS Images (AMI)

1. Click **Ubuntu** (or search "Ubuntu")
2. Select **Ubuntu Server 22.04 LTS (HVM), SSD Volume Type**
3. Architecture: **64-bit (x86)** — default
4. Keep **Free tier eligible** if shown

#### Section: Instance type

1. Click the instance type dropdown
2. Select **t2.micro** (should show "Free tier eligible")
3. If t2.micro is not listed, pick **t3.micro** (also free tier eligible)

#### Section: Key pair (login)

1. Click the **Key pair** dropdown
2. Select **acc-demo-deploy** (the key you created in Step 3.2)
3. Do **not** select "Proceed without a key pair"

#### Section: Network settings

Click **Edit** on the right side of this section.

| Setting | What to choose |
|---------|----------------|
| **VPC** | Leave default |
| **Subnet** | Leave default |
| **Auto-assign public IP** | **Enable** |
| **Firewall (security groups)** | Select **Create security group** |

Under **Inbound Security Group Rules**, you need **two rules**:

**Rule 1 — SSH (for you and GitHub deploy):**

| Column | Value |
|--------|-------|
| Type | **SSH** |
| Port | 22 (auto-filled) |
| Source type | **My IP** (recommended) |
| Source | Auto-fills your current public IP |

> **My IP** means only your computer can SSH. For GitHub Actions deploy, GitHub runs from changing IPs — see [Section 11](#11-troubleshooting) if deploy fails on SSH. For first-time setup, **My IP** is fine; you can widen SSH later.

**Rule 2 — HTTP (for the demo website):**

Click **Add security group rule**

| Column | Value |
|--------|-------|
| Type | **HTTP** |
| Port | 80 |
| Source type | **Anywhere** |
| Source | `0.0.0.0/0` |

**Optional Rule 3 — HTTPS (only if you add a domain later):**

| Type | HTTPS | Port 443 | Source Anywhere `0.0.0.0/0` |

Security group name: AWS may auto-name it `launch-wizard-1`. You can change it to **`acc-demo-sg`**.

#### Section: Configure storage

| Setting | Value |
|---------|-------|
| Size | **30** GiB (or 8 GiB minimum — 30 is fine for free tier) |
| Volume type | **gp3** |
| Delete on termination | Leave checked (default) |

#### Section: Advanced details

Leave everything default. **Do not** change anything here for the demo.

#### Launch

1. On the right panel, check **Number of instances** = **1**
2. Review the summary
3. Click the orange **Launch instance** button
4. You should see "Successfully initiated launch of instance"
5. Click **View all instances** (or go to EC2 → Instances)

#### Wait until the instance is ready

1. On the **Instances** page, find `acc-demo-server`
2. Wait until **Instance state** = **Running** (refresh the page every 10–20 seconds)
3. Wait until **Status check** = **2/2 checks passed** (may take 1–2 minutes)

4. Click the instance row → in the bottom **Details** tab, find **Public IPv4 address**
   - Example: `54.123.45.67`
   - **Write this down** — you will use it until you attach an Elastic IP

---

### Step 3.4 — Quick test: can you reach the server?

You have not installed the app yet, so HTTP will not work. But the instance should be **Running**.

Checklist before moving on:

- [ ] Instance state = **Running**
- [ ] Status check = **2/2 checks passed**
- [ ] You have `acc-demo-deploy.pem` saved on your PC
- [ ] Security group allows SSH (22) and HTTP (80)
- [ ] You noted the **Public IPv4 address**

Next: [Section 4](#4-elastic-ip--click-by-click) — attach a permanent Elastic IP.

---

## 4. Elastic IP — click-by-click

A normal EC2 public IP **changes** when you stop/start the instance. An **Elastic IP** stays the same — required for GitHub secrets.

### Step 4.1 — Allocate an Elastic IP

1. AWS Console → **EC2**
2. Left sidebar → **Network & Security** → **Elastic IPs**
3. Click **Allocate Elastic IP address** (orange button)
4. Settings:

| Field | Value |
|-------|-------|
| **Network border group** | Leave default |
| **Public IPv4 address pool** | Amazon's pool of IPv4 addresses |
| **Tags** | Optional: Name = `acc-demo-eip` |

5. Click **Allocate**
6. You see a new row with an **Elastic IP address** (e.g. `3.85.1.2`)
7. **Copy this IP** — you will use it for:
   - SSH: `ssh -i acc-demo-deploy.pem ubuntu@3.85.1.2`
   - GitHub secrets: `DEMO_HOST` and `DEMO_PUBLIC_HOST`

### Step 4.2 — Associate Elastic IP with your instance

1. Select the Elastic IP row (checkbox)
2. Click **Actions** → **Associate Elastic IP address**
3. Fill in:

| Field | Value |
|-------|-------|
| **Resource type** | Instance |
| **Instance** | Select **acc-demo-server** from dropdown |
| **Private IP address** | Leave default (auto-selected) |
| **Reassociation** | Leave default |

4. Click **Associate**
5. Go to **Instances** → click `acc-demo-server` → confirm **Public IPv4 address** now matches your Elastic IP

### How GitHub Actions uses this IP

The workflow file does **not** contain the IP. You paste it into GitHub Secrets manually:

| GitHub secret | Value |
|---------------|-------|
| `DEMO_HOST` | Your Elastic IP (e.g. `3.85.1.2`) |
| `DEMO_PUBLIC_HOST` | Same IP (or your domain later) |

```
You allocate Elastic IP in AWS Console
        ↓
You paste IP into GitHub secrets DEMO_HOST + DEMO_PUBLIC_HOST
        ↓
deploy job: ssh ubuntu@DEMO_HOST
        ↓
Code lands on /opt/acc-demo/app/
```

### Free tier note

- Elastic IP **attached** to a **running** instance: **no extra charge**
- Elastic IP **not attached** to a running instance: small monthly fee — **release** it if you delete the server

---

## 5. SSH into your server (Windows)

You need to connect to the server to run setup commands. Use **PowerShell** on Windows.

### Step 5.1 — Fix `.pem` permissions (Windows, one time)

Open **PowerShell** and run (adjust the path to where you saved the file):

```powershell
cd $env:USERPROFILE\.ssh
# If the file is elsewhere, cd to that folder first
icacls acc-demo-deploy.pem /inheritance:r
icacls acc-demo-deploy.pem /grant:r "$($env:USERNAME):(R)"
```

### Step 5.2 — Connect via SSH

Replace `3.85.1.2` with **your Elastic IP**:

```powershell
ssh -i C:\Users\YourName\.ssh\acc-demo-deploy.pem ubuntu@3.85.1.2
```

| Part | Meaning |
|------|---------|
| `-i ...pem` | Your private key file |
| `ubuntu` | Default username for Ubuntu AMI (not `root`, not `ec2-user`) |
| `3.85.1.2` | Your Elastic IP |

**First connection:** type `yes` when asked "Are you sure you want to continue connecting?"

You should see a prompt like:

```
ubuntu@ip-172-31-xx-xx:~$
```

You are now **inside** the Linux server. All commands in [Section 7](#7-one-time-ec2-server-setup-commands) run here.

### Step 5.3 — Disconnect

Type `exit` and press Enter to close SSH.

---

## 6. Domain registration (optional)

You can skip DNS for the first smoke test and use **http://&lt;ELASTIC_IP&gt;/** directly.

### Phase 1 — IP only (fastest — recommended for first test)

1. Nginx `server_name _` (underscore = accept any hostname)
2. Browse `http://3.85.1.2/` (your Elastic IP)
3. GitHub: `DEMO_PUBLIC_HOST=3.85.1.2`

### Phase 2 — Add a hostname (optional)

**Path A — Company subdomain (ask IT)**

1. Ask IT to create DNS **A record**: `acc-demo.yourcompany.com` → your Elastic IP
2. On server: update Nginx `server_name acc-demo.yourcompany.com`
3. Run Certbot: `sudo certbot --nginx -d acc-demo.yourcompany.com`
4. GitHub: `DEMO_PUBLIC_HOST=acc-demo.yourcompany.com`

**Path B — Route 53 (AWS DNS)**

1. AWS Console search → **Route 53**
2. Left sidebar → **Registered domains** → **Register domains** (or use existing hosted zone)
3. Left sidebar → **Hosted zones** → click your domain
4. Click **Create record**:

| Field | Value |
|-------|-------|
| Record name | `acc-demo` (creates `acc-demo.yourdomain.com`) |
| Record type | **A** |
| Value | Your Elastic IP |
| TTL | 300 |

5. Click **Create records**
6. Wait 5–30 minutes for DNS propagation
7. Test: `nslookup acc-demo.yourdomain.com`

| Where | Example |
|-------|---------|
| DNS A record | `acc-demo.yourcompany.com` → Elastic IP |
| Nginx `server_name` | `acc-demo.yourcompany.com` |
| GitHub `DEMO_PUBLIC_HOST` | `acc-demo.yourcompany.com` |

---

## 7. One-time EC2 server setup (commands)

**Prerequisite:** You are SSH'd into the server ([Section 5](#5-ssh-into-your-server-windows)).

Run these commands **in order** on the server (copy-paste each block).

### 7.1 Packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip nginx git rsync
```

Add Certbot only if using a domain (Phase 2 above):

```bash
sudo apt install -y certbot python3-certbot-nginx
```

### 7.2 App user and directories

```bash
sudo useradd --system --home /opt/acc-demo --shell /usr/sbin/nologin accdemo || true
sudo mkdir -p /opt/acc-demo/app /var/lib/acc-demo
sudo chown -R accdemo:accdemo /opt/acc-demo /var/lib/acc-demo
```

### 7.3 First code drop

Either clone from GitHub or wait for first CI deploy. Manual clone (replace with your demo repo URL if different):

```bash
sudo -u accdemo git clone https://github.com/YOUR-USERNAME/acc-hosting-demo.git /opt/acc-demo/repo
sudo -u accdemo cp -r /opt/acc-demo/repo/* /opt/acc-demo/app/
```

Or copy from this monorepo if you cloned it:

```bash
sudo -u accdemo git clone https://github.com/cctech-labs/FormaDatabricksIntegration.git /opt/acc-demo/repo
sudo -u accdemo mkdir -p /opt/acc-demo/app
sudo -u accdemo cp -r /opt/acc-demo/repo/acc-connector/demo/* /opt/acc-demo/app/
```

### 7.4 Virtualenv

```bash
sudo -u accdemo python3.11 -m venv /opt/acc-demo/venv
sudo -u accdemo /opt/acc-demo/venv/bin/pip install -r /opt/acc-demo/app/requirements.txt
```

### 7.5 `.env`

```bash
sudo -u accdemo cp /opt/acc-demo/app/.env.example /opt/acc-demo/app/.env
sudo nano /opt/acc-demo/app/.env
```

In `nano`: set `SECRET_KEY` (run `python3 -c "import secrets; print(secrets.token_hex(32))"` to generate). Press **Ctrl+O** to save, **Ctrl+X** to exit.

```bash
sudo chmod 600 /opt/acc-demo/app/.env
```

### 7.6 Persistent `connector.db`

```bash
sudo -u accdemo touch /var/lib/acc-demo/connector.db
sudo rm -f /opt/acc-demo/app/connector.db
sudo -u accdemo ln -s /var/lib/acc-demo/connector.db /opt/acc-demo/app/connector.db
```

### 7.7 systemd (Gunicorn)

```bash
sudo cp /opt/acc-demo/app/deploy/demo-app.service /etc/systemd/system/acc-demo.service
sudo systemctl daemon-reload
sudo systemctl enable acc-demo
sudo systemctl start acc-demo
curl -s http://127.0.0.1:8001/health
```

Expected: `{"status":"ok","app":"hosting-demo"}`

If it fails: `sudo journalctl -u acc-demo -n 50 --no-pager`

### 7.8 Nginx

```bash
sudo cp /opt/acc-demo/app/deploy/nginx-demo.conf /etc/nginx/sites-available/acc-demo
sudo nano /etc/nginx/sites-available/acc-demo
```

Change `YOUR_DOMAIN` to `_` (underscore) for IP-only access, or your domain name.

```bash
sudo ln -s /etc/nginx/sites-available/acc-demo /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 7.9 Test in browser

On your PC, open: `http://<YOUR_ELASTIC_IP>/`

You should see the demo page with `.env` and `connector.db` tables.

### 7.10 SSH key for GitHub Actions (for automated deploy)

GitHub Actions needs SSH access. Use the **same** `.pem` key:

1. On your PC, open `acc-demo-deploy.pem` in Notepad
2. Copy the **entire** contents (including `-----BEGIN RSA PRIVATE KEY-----` lines)
3. Paste into GitHub secret `SSH_PRIVATE_KEY` ([Section 8](#8-github--click-by-click))

Alternatively, add a separate deploy public key to the server:

```bash
echo "ssh-ed25519 AAAA... your-comment" >> ~/.ssh/authorized_keys
```

---

## 8. GitHub — click-by-click

### Step 8.1 — Create your demo repository

1. Go to [https://github.com](https://github.com) → sign in to your **demo/personal** account
2. Click **+** (top right) → **New repository**
3. Fill in:

| Field | Value |
|-------|-------|
| **Repository name** | `acc-hosting-demo` |
| **Description** | Optional |
| **Public / Private** | Your choice |
| **Add a README** | Uncheck (you will upload files) |
| **Add .gitignore** | None |
| **Choose a license** | None |

4. Click **Create repository**

### Step 8.2 — Upload demo files

Copy these from `acc-connector/demo/` in this project into your new repo:

| Copy from | To repo path |
|-----------|--------------|
| `demo/app.py` | `app.py` |
| `demo/requirements.txt` | `requirements.txt` |
| `demo/.env.example` | `.env.example` |
| `demo/deploy/` folder | `deploy/` |
| `demo/demo-hosting-ci.yml` | `.github/workflows/demo-hosting-ci.yml` |

**Using GitHub web UI:**

1. In your repo, click **Add file** → **Upload files**
2. Drag `app.py`, `requirements.txt`, `.env.example`
3. Click **Commit changes**
4. Click **Add file** → **Create new file**
5. Type `.github/workflows/demo-hosting-ci.yml` in the name box (GitHub creates folders automatically)
6. Paste contents from [demo/demo-hosting-ci.yml](demo/demo-hosting-ci.yml)
7. Click **Commit changes**
8. Upload `deploy/` files similarly

**Or using git on your PC:**

```powershell
git clone https://github.com/YOUR-USERNAME/acc-hosting-demo.git
cd acc-hosting-demo
# copy files into repo root
git add .
git commit -m "Initial demo hosting app"
git push origin main
```

### Step 8.3 — Create GitHub Environment (optional)

1. Open your repo on GitHub
2. Click **Settings** tab
3. Left sidebar → **Environments**
4. Click **New environment**
5. Name: `demo`
6. Click **Configure environment**
7. Leave **Required reviewers** empty for POC
8. Click **Save protection rules** (or just save)

### Step 8.4 — Add repository variable

1. Repo **Settings** → **Secrets and variables** → **Actions**
2. Click the **Variables** tab
3. Click **New repository variable**

| Name | Value |
|------|-------|
| `ENABLE_DEMO_DEPLOY` | `true` |

4. Click **Add variable**

### Step 8.5 — Add repository secrets

1. Same page → **Secrets** tab → **New repository secret**
2. Add each secret one at a time:

| Secret name | What to paste | Where to get it |
|-------------|---------------|-----------------|
| `SSH_PRIVATE_KEY` | Full contents of `acc-demo-deploy.pem` | File from Step 3.2 |
| `DEPLOY_USER` | `ubuntu` | Always `ubuntu` for Ubuntu AMI |
| `DEMO_HOST` | `3.85.1.2` | Your Elastic IP from Step 4.1 |
| `DEMO_PUBLIC_HOST` | `3.85.1.2` | Same Elastic IP (or domain later) |

> **SSH_PRIVATE_KEY tip:** Open `.pem` in Notepad → Select All → Copy → paste into secret. Include the BEGIN/END lines.

### Step 8.6 — Trigger first deploy

1. Make a small change in your demo repo (e.g. edit `DEPLOY_LABEL` in `.env.example` on server only, or change a comment in `app.py`)
2. Commit and push to **`main`** branch
3. Click **Actions** tab in GitHub
4. You should see workflow **Demo Hosting CI/CD** running
5. Wait for **test** (green) then **deploy** (green)

If deploy is **skipped**: check `ENABLE_DEMO_DEPLOY` is `true` and you pushed to `main` (not a PR only).

### Workflow file location

**[demo/demo-hosting-ci.yml](demo/demo-hosting-ci.yml)** — lives in your demo repo at `.github/workflows/demo-hosting-ci.yml`

| Job | When it runs |
|-----|----------------|
| `test` | Compile `app.py` on every push/PR to `main` |
| `deploy` | After `test`, when `ENABLE_DEMO_DEPLOY=true` and push to `main` |

---

## 9. Verify the demo

| # | Check | Expected |
|---|-------|----------|
| 1 | `curl http://<ELASTIC_IP>/health` | `{"status":"ok","app":"hosting-demo"}` |
| 2 | Open homepage in browser | Tables for `.env` and `connector.db` |
| 3 | Refresh 3 times | `visit_count` increases |
| 4 | Push code change, wait for deploy | Page updates; visit count **unchanged** |
| 5 | GitHub Actions | `test` + `deploy` green |

---

## 10. Graduate to Staging

When the demo passes:

| Demo (free tier) | Staging (real connector) |
|------------------|--------------------------|
| t2.micro / t3.micro | **t3.small** |
| [demo/app.py](demo/app.py) | [app.py](app.py) |
| `/opt/acc-demo` | `/opt/acc-connector` |
| `ENABLE_DEMO_DEPLOY` | `ENABLE_DEPLOY` |
| `DEMO_HOST` | `STAGING_HOST` |
| HTTP or simple domain | HTTPS + APS + Databricks OAuth |

Follow [STAGING_HOSTING_GUIDE.md](STAGING_HOSTING_GUIDE.md) next. **Stop or terminate** the demo EC2 to save free tier hours when done.

---

## 11. Troubleshooting

| Problem | Likely cause | Fix |
|---------|--------------|-----|
| **Cannot SSH** | Wrong IP, wrong `.pem`, port 22 blocked | Check Elastic IP; user must be `ubuntu`; security group allows SSH from your IP |
| **Permission denied (publickey)** | Wrong key file or wrong user | Use `-i acc-demo-deploy.pem` and user `ubuntu` |
| **Connection timed out** | Security group or instance not running | EC2 → Instances → Running; SG allows port 22 from your IP |
| **502 Bad Gateway** in browser | Gunicorn not running | `sudo systemctl status acc-demo`; `sudo journalctl -u acc-demo -n 50` |
| **Blank page / connection refused** | Nginx not running or wrong config | `sudo systemctl status nginx`; `sudo nginx -t` |
| **GitHub deploy SSH fails** | GitHub IPs not allowed on port 22 | Temporarily change SG rule 22 source to `0.0.0.0/0` for POC only, or use a self-hosted runner |
| **GitHub deploy skipped** | Variable not set or wrong branch | `ENABLE_DEMO_DEPLOY=true`; push to `main` not just PR |
| **curl health fails in deploy** | App not up or wrong `DEMO_PUBLIC_HOST` | SSH in, run `curl http://127.0.0.1:8001/health`; fix Nginx |
| **visit_count resets after deploy** | `connector.db` not symlinked | Redo [Section 7.6](#76-persistent-connectordb) |
| **Lost `.pem` file** | Cannot recover | Create new instance with new key pair |

### Widen SSH for GitHub Actions (POC only)

GitHub Actions runs from Microsoft-hosted runners with **changing IP addresses**. If deploy fails at SSH:

1. EC2 → **Security Groups** → select `acc-demo-sg`
2. **Edit inbound rules**
3. Find SSH rule → change Source from **My IP** to **Anywhere** (`0.0.0.0/0`)
4. Save rules

> **Security warning:** Opening SSH to the world is OK for a short POC demo only. For Staging/production, restrict SSH to your office IP or use a VPN.

### Useful server commands

```bash
sudo systemctl status acc-demo
sudo journalctl -u acc-demo -f
sudo systemctl status nginx
curl -s http://127.0.0.1:8001/health
free -h
```

---

## Quick checklist

**AWS (before GitHub):**

- [ ] Create key pair `acc-demo-deploy.pem` → save safely
- [ ] Launch t2.micro Ubuntu with security group (22 + 80)
- [ ] Allocate and associate **Elastic IP**
- [ ] SSH works: `ssh -i acc-demo-deploy.pem ubuntu@<ELASTIC_IP>`
- [ ] Complete [Section 7](#7-one-time-ec2-server-setup-commands)
- [ ] Browse `http://<ELASTIC_IP>/` — visit counter works

**GitHub:**

- [ ] Demo repo created with app files + workflow
- [ ] `ENABLE_DEMO_DEPLOY=true`
- [ ] Secrets: `SSH_PRIVATE_KEY`, `DEPLOY_USER`, `DEMO_HOST`, `DEMO_PUBLIC_HOST`
- [ ] Push to `main` → `test` + `deploy` green

**Optional:**

- [ ] Register domain → A record → Elastic IP → Certbot HTTPS

---

*Guide version: June 2026 — AWS Free Tier demo with beginner click-by-click steps.*
