# Staging EC2 — Continue Setup (from clone on server)

> **Use this guide when:** The `accconnector` user and `/opt/acc-connector` directories exist on EC2, but app setup is not finished yet. Run the diagnostic in [Section 1](#1-check-server-state) first — it tells you whether to clone, move from `/tmp`, or only recreate the symlink.
>
> **Staging values:**
>
> | Item | Value |
> |------|-------|
> | Hostname | `formabricks-stg.cctech.co.in` |
> | Elastic IP | `32.197.105.164` |
> | Deploy branch | `staging-deployment` |
> | App path | `/opt/acc-connector/app` |
>
> **Related:** [STAGING_AWS_HOSTING_GUIDE.md](STAGING_AWS_HOSTING_GUIDE.md) (full guide from scratch)

---

## Table of Contents

0. [SSH access and security group (read first)](#0-ssh-access-and-security-group-read-first)
1. [Check server state](#1-check-server-state)
2. [Get code onto the server and create app symlink](#2-get-code-onto-the-server-and-create-app-symlink)
3. [Python virtual environment](#3-python-virtual-environment)
4. [Server `.env`](#4-server-env)
5. [Persistent `connector.db`](#5-persistent-connectordb)
6. [systemd service (Gunicorn)](#6-systemd-service-gunicorn)
7. [Nginx + HTTPS](#7-nginx--https)
8. [Firewall (optional)](#8-firewall-optional)
9. [GitHub Actions SSH access](#9-github-actions-ssh-access)
10. [Enable CI/CD deploy](#10-enable-cicd-deploy)
11. [First deploy and verification](#11-first-deploy-and-verification)
12. [Troubleshooting](#12-troubleshooting)
13. [Cost estimate](#13-cost-estimate)
14. [Quick start checklist](#14-quick-start-checklist)

---

## 0. SSH access and security group (read first)

If SSH times out from your PC:

```text
ssh: connect to host 32.197.105.164 port 22: Connection timed out
```

**Ping also timing out is normal.** AWS security groups do not allow ICMP unless you add a rule. Ping failure does **not** mean the server is down.

### Your security group today

For `acc-connector-stg`:

| Port | Source | Purpose |
|------|--------|---------|
| 22 | `103.8.39.122/32` only | SSH — **your PC must use this IP** |
| 80 | `0.0.0.0/0`, `::/0` | HTTP |
| 443 | `0.0.0.0/0`, `::/0` | HTTPS |

If your current public IP is **not** `103.8.39.122`, SSH will always time out until you update the rule.

### Fix — update SSH rule to your current IP

**Step 1 — find your public IP** (on your Windows PC):

```powershell
(Invoke-WebRequest -Uri "https://api.ipify.org" -UseBasicParsing).Content
```

Example output: `223.185.38.102` (yours will differ).

**Step 2 — AWS Console**

1. **EC2** → **Instances** → confirm `acc-connector-staging` (or similar) is **Running**
2. **Elastic IP** `32.197.105.164` is **associated** with that instance
3. **Security Groups** → `acc-connector-stg` → **Edit inbound rules**
4. Find the **SSH (22)** rule with source `103.8.39.122/32`
5. Change **Source** to **My IP** (or paste `YOUR_IP/32` from Step 1)
6. **Save rules**
7. Retry SSH:

```powershell
ssh -i D:\DatabricksPOC\14.CleanCodeG\FormaDatabricksIntegration\acc-connector-stg.pem ubuntu@32.197.105.164
```

### Real-project pattern (dynamic IP at home/office)

Do **not** leave SSH open to `0.0.0.0/0` long-term. Use one of these:

| Approach | Admin SSH (you) | CI deploy (GitHub Actions) |
|----------|-----------------|----------------------------|
| **A — Update IP when it changes** | Edit SG rule to your current `/32` | Add a **second** SSH rule for GitHub (see below) or use a self-hosted runner |
| **B — Office / VPN CIDR** | Allow e.g. `203.0.113.0/24` (company VPN) | Runner in same VPC or widen SSH only during deploy |
| **C — AWS Systems Manager Session Manager** | No port 22 from internet; connect via AWS Console / CLI | Still needs rsync alternative or SSM-based deploy |

**When your home ISP changes IP** (dynamic IP): repeat Step 1–2 whenever SSH starts timing out again.

### GitHub Actions deploy + locked-down SSH

GitHub-hosted runners use **changing public IPs**. If SSH is only `103.8.39.122/32`, **deploy will fail** even if you can SSH from that one machine.

Options for Staging POC:

1. **Temporary:** add inbound SSH `0.0.0.0/0` only while testing deploy, then remove
2. **Better:** add a **second** inbound rule — SSH from your `/32` **and** a wider range only if your org allows it
3. **Best for production:** self-hosted GitHub runner on the VPC, or CodeDeploy / ECR image pull without SSH

See [Section 9](#9-github-actions-ssh-access) and [Section 12](#12-troubleshooting).

### Still timing out after updating IP?

Check:

| Check | Where | Expected |
|-------|-------|----------|
| Instance state | EC2 → Instances | **Running** |
| Elastic IP | EC2 → Elastic IPs | `32.197.105.164` → attached to instance |
| Correct SG | Instance → Security tab | `acc-connector-stg` attached |
| Correct key | Local `.pem` | Same key pair used at launch |
| Local firewall | Corporate laptop VPN | Try off VPN or allow outbound 22 |

---

## 1. Check server state

Run this on EC2 **before** any move or clone:

```bash
sudo ls -la /opt/acc-connector/
ls /opt/acc-connector/repo/acc-connector/app.py 2>/dev/null && echo "REPO OK" || echo "NO REPO"
ls /tmp/repo/acc-connector/app.py 2>/dev/null && echo "TMP CLONE OK" || echo "NO TMP CLONE"
readlink -f /opt/acc-connector/app 2>/dev/null || echo "NO APP SYMLINK"
```

### How to read the output

| What you see | Meaning | Next step |
|--------------|---------|-----------|
| `/opt/acc-connector/` shows only `.` and `..` (empty) | User/dirs exist; **no code yet** | [Section 2A](#2a-clone-directly-to-opt--most-common) |
| `NO REPO` and `NO TMP CLONE` | Nothing to move; `/tmp` was cleared (reboot, etc.) | [Section 2A](#2a-clone-directly-to-opt--most-common) |
| `TMP CLONE OK` but `NO REPO` | Clone still in `/tmp/repo` | [Section 2B](#2b-move-from-tmprepo) |
| `REPO OK` | Git repo already under `/opt/acc-connector/repo` | [Section 2C](#2c-symlink-only) |

**Example — empty `/opt/acc-connector` (your current state):**

```text
ubuntu@ip-172-31-93-228:~$ sudo ls -la /opt/acc-connector/
total 8
drwxr-xr-x 2 accconnector accconnector 4096 Jun 25 13:57 .
drwxr-xr-x 3 root         root         4096 Jun 25 12:28 ..
```

→ Use **Section 2A**. Do **not** run `sudo mv /tmp/repo ...` — that path is gone.

Ensure system packages are installed (Section 6.1 of the main guide):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git rsync
python3.11 --version
```

**Path tip:** use absolute paths with a leading `/` — e.g. `cd /opt/acc-connector/app`, not `cd opt`.

---

## 2. Get code onto the server and create app symlink

All paths need the repo at `/opt/acc-connector/repo` and a symlink at `/opt/acc-connector/app` → `repo/acc-connector`.

First, ensure the app user and directories exist (safe to re-run):

```bash
sudo useradd --system --home /opt/acc-connector --shell /usr/sbin/nologin accconnector 2>/dev/null || true
sudo mkdir -p /opt/acc-connector /var/lib/acc-connector
sudo chown -R accconnector:accconnector /opt/acc-connector /var/lib/acc-connector
```

Then follow **one** subsection below (2A, 2B, or 2C).

### 2A. Clone directly to `/opt` — most common

Use when `/opt/acc-connector` is empty or both `NO REPO` and `NO TMP CLONE`.

**Important:** The GitHub deploy key lives in `/home/ubuntu/.ssh/`. The `accconnector` user **cannot** read another user's private key. Clone **as `ubuntu`**, then move and fix ownership.

```bash
sudo rm -rf /opt/acc-connector/repo
sudo rm -f /opt/acc-connector/app

# Clone as ubuntu (owns github_deploy) into /tmp first
GIT_SSH_COMMAND='ssh -i /home/ubuntu/.ssh/github_deploy -o StrictHostKeyChecking=accept-new' \
  git clone --branch staging-deployment \
  git@github.com:cctech-labs/FormaDatabricksIntegration.git /tmp/repo-clone

# Move into deploy layout and hand off to accconnector
sudo mv /tmp/repo-clone /opt/acc-connector/repo
sudo chown -R accconnector:accconnector /opt/acc-connector/repo
sudo -u accconnector ln -sfn /opt/acc-connector/repo/acc-connector /opt/acc-connector/app
```

Verify:

```bash
cd /opt/acc-connector/app && pwd && ls app.py
```

If clone fails with **`Identity file ... not accessible: Permission denied`** — you ran `sudo -u accconnector` with ubuntu's key. Use the commands above (clone as **ubuntu**, not `accconnector`).

If clone fails with **`Permission denied (publickey)`** and no "not accessible" warning:

```bash
ls -la /home/ubuntu/.ssh/github_deploy
# Must exist, mode 600, owner ubuntu

# Test GitHub SSH as ubuntu
GIT_SSH_COMMAND='ssh -i /home/ubuntu/.ssh/github_deploy -o StrictHostKeyChecking=accept-new' \
  ssh -T git@github.com
# Expect: "Hi cctech-labs/FormaDatabricksIntegration! You've successfully authenticated..."
```

If the key test fails, the **public** half is not registered in GitHub → **Settings** → **Deploy keys** → add `/home/ubuntu/.ssh/github_deploy.pub` (or re-create the key pair and add the new public key).

Fix key file permissions if needed:

```bash
chmod 700 /home/ubuntu/.ssh
chmod 600 /home/ubuntu/.ssh/github_deploy
chown -R ubuntu:ubuntu /home/ubuntu/.ssh
```

If you do not have `/home/ubuntu/.ssh/github_deploy`, use HTTPS instead (public repo only, or use a PAT for private repos):

```bash
sudo rm -rf /opt/acc-connector/repo
sudo -u accconnector git clone --branch staging-deployment \
  https://github.com/cctech-labs/FormaDatabricksIntegration.git /opt/acc-connector/repo
sudo chown -R accconnector:accconnector /opt/acc-connector/repo
sudo -u accconnector ln -sfn /opt/acc-connector/repo/acc-connector /opt/acc-connector/app
```

### 2B. Move from `/tmp/repo`

Use only when `ls /tmp/repo/acc-connector/app.py` succeeds:

```bash
sudo rm -f /opt/acc-connector/app
sudo rm -rf /opt/acc-connector/repo
sudo mv /tmp/repo /opt/acc-connector/repo
sudo chown -R accconnector:accconnector /opt/acc-connector/repo
sudo -u accconnector ln -sfn /opt/acc-connector/repo/acc-connector /opt/acc-connector/app
```

If `mv: cannot stat '/tmp/repo'`, `/tmp` was cleared — use [Section 2A](#2a-clone-directly-to-opt--most-common) instead.

### 2C. Symlink only

Use when `ls /opt/acc-connector/repo/acc-connector/app.py` already succeeds:

```bash
sudo rm -f /opt/acc-connector/app
sudo -u accconnector ln -sfn /opt/acc-connector/repo/acc-connector /opt/acc-connector/app
```

### Verify (all paths)

```bash
ls -la /opt/acc-connector/
readlink -f /opt/acc-connector/app
cd /opt/acc-connector/app && pwd && ls
```

Expected:

- `readlink -f` prints `/opt/acc-connector/repo/acc-connector`
- `pwd` prints `/opt/acc-connector/app`
- Directory lists `app.py`, `backend/`, `deploy/`, `requirements.txt`, etc.

---

## 3. Python virtual environment

```bash
cd /opt/acc-connector/app
sudo -u accconnector python3.11 -m venv /opt/acc-connector/venv
sudo -u accconnector /opt/acc-connector/venv/bin/pip install --upgrade pip
sudo -u accconnector /opt/acc-connector/venv/bin/pip install -r requirements.txt
```

---

## 4. Server `.env`

Create **one** Staging `.env` on the server. Testers share this file; per-user state lives in `connector.db`.

Generate a `SECRET_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Create the file (replace placeholders with real Staging credentials):

```bash
sudo nano /opt/acc-connector/app/.env
```

Example Staging `.env` (HTTPS URLs — no trailing slash):

```ini
SECRET_KEY=<unique-32-byte-hex>
APS_CLIENT_ID=<staging-aps-client-id>
APS_CLIENT_SECRET=<staging-aps-secret>
APS_REDIRECT_URI=https://formabricks-stg.cctech.co.in/callback
DATABRICKS_CLIENT_ID=<staging-dbx-oauth-client-id>
DATABRICKS_CLIENT_SECRET=<staging-dbx-oauth-client-secret>
DATABRICKS_REDIRECT_URI=https://formabricks-stg.cctech.co.in/databricks/callback
```

Lock down permissions:

```bash
sudo chown accconnector:accconnector /opt/acc-connector/app/.env
sudo chmod 600 /opt/acc-connector/app/.env
```

### Hostname checklist — must match exactly

| Where | Value |
|-------|-------|
| DNS A record | `formabricks-stg.cctech.co.in` → `32.197.105.164` |
| Nginx `server_name` | `formabricks-stg.cctech.co.in` |
| Certbot `-d` flag | `formabricks-stg.cctech.co.in` |
| `.env` → `APS_REDIRECT_URI` | `https://formabricks-stg.cctech.co.in/callback` |
| `.env` → `DATABRICKS_REDIRECT_URI` | `https://formabricks-stg.cctech.co.in/databricks/callback` |
| APS app callback URL | `https://formabricks-stg.cctech.co.in/callback` |
| Databricks OAuth redirect URI | `https://formabricks-stg.cctech.co.in/databricks/callback` |
| GitHub secret `STAGING_PUBLIC_HOST` | `formabricks-stg.cctech.co.in` |
| GitHub secret `STAGING_HOST` | `32.197.105.164` |

**Rules:**

- Never commit `.env` to git.
- Do not rotate `SECRET_KEY` casually — it invalidates encrypted tokens in `connector.db`.
- CI deploy never overwrites `.env` (`rsync --exclude '.env'`).

See [.env.example](.env.example) for optional feature flags.

---

## 5. Persistent `connector.db`

`connector.db` stores encrypted tokens and per-user state. It must survive code deploys.

```bash
sudo -u accconnector touch /var/lib/acc-connector/connector.db
sudo rm -f /opt/acc-connector/app/connector.db
sudo -u accconnector ln -s /var/lib/acc-connector/connector.db /opt/acc-connector/app/connector.db
sudo chown accconnector:accconnector /var/lib/acc-connector/connector.db
```

Why: GitHub Actions `rsync` updates `/opt/acc-connector/app/` but excludes `connector.db`. The real file lives in `/var/lib/acc-connector/` so deploys never wipe tester tokens or bootstrap state.

---

## 6. systemd service (Gunicorn)

```bash
sudo cp /opt/acc-connector/app/deploy/acc-connector.service /etc/systemd/system/acc-connector.service
sudo systemctl daemon-reload
sudo systemctl enable acc-connector
sudo systemctl start acc-connector
sudo systemctl status acc-connector
```

Verify Gunicorn responds locally:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/
# Expect: 200
```

The unit uses `--workers 2` and `--timeout 600` — appropriate for t3.small and long sync runs.

If the service fails, check logs before continuing:

```bash
sudo journalctl -u acc-connector -n 50 --no-pager
```

Common first-time failures: missing `.env`, wrong permissions on `.env`, or pip install not completed.

---

## 7. Nginx + HTTPS

Copy the Nginx config and set `server_name` to `formabricks-stg.cctech.co.in`:

```bash
sudo cp /opt/acc-connector/app/deploy/nginx-acc-connector.conf /etc/nginx/sites-available/acc-connector
sudo sed -i 's/YOUR_DOMAIN/formabricks-stg.cctech.co.in/g' /etc/nginx/sites-available/acc-connector
sudo ln -sf /etc/nginx/sites-available/acc-connector /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

sudo certbot --nginx -d formabricks-stg.cctech.co.in
```

Optional — password-protect Staging (uncomment `auth_basic` lines in the Nginx config first):

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd qa_tester
sudo nginx -t && sudo systemctl reload nginx
```

Verify HTTPS:

```bash
curl -I https://formabricks-stg.cctech.co.in/
curl -sf https://formabricks-stg.cctech.co.in/health
```

---

## 8. Firewall (optional)

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

---

## 9. GitHub Actions SSH access

CI deploy connects as the `ubuntu` user. Ensure the deploy private key can SSH in.

**Option A — same key as manual SSH:** paste the contents of `acc-connector-deploy.pem` into GitHub secret `SSH_PRIVATE_KEY`.

**Option B — dedicated deploy key:** add the public key on EC2:

```bash
# On EC2, as ubuntu user:
echo "<github-actions-public-key>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Test from your laptop (replace key path if needed):

```bash
ssh -i acc-connector-deploy.pem ubuntu@32.197.105.164 "echo OK"
```

---

## 10. Enable CI/CD deploy

The workflow at [`.github/workflows/acc-connector-ci.yml`](../.github/workflows/acc-connector-ci.yml) deploys Staging on push to `staging-deployment` when deploy is enabled.

### Step 1 — GitHub repository variable

GitHub → **Settings** → **Secrets and variables** → **Actions** → **Variables**:

| Name | Value |
|------|-------|
| `ENABLE_DEPLOY` | `true` |

### Step 2 — GitHub secrets (Staging only)

GitHub → **Settings** → **Secrets and variables** → **Actions** → **Secrets**:

| Secret | Your value | Used for |
|--------|------------|----------|
| `SSH_PRIVATE_KEY` | Contents of `acc-connector-deploy.pem` | SSH/rsync to EC2 |
| `DEPLOY_USER` | `ubuntu` | SSH user on EC2 |
| `STAGING_HOST` | `32.197.105.164` | SSH target |
| `STAGING_PUBLIC_HOST` | `formabricks-stg.cctech.co.in` | Post-deploy `curl /health` |

Not needed now: `DEV_HOST`, `DEV_PUBLIC_HOST`, `PROD_HOST`, `PROD_PUBLIC_HOST`.

### Step 3 — GitHub Environment

Create environment **`staging`** (Settings → Environments). No required reviewers needed for a POC.

### What deploy does

On push to `staging-deployment` (after tests pass):

```bash
rsync -avz --delete \
  --exclude '.env' \
  --exclude 'connector.db' \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  ./ ubuntu@32.197.105.164:/opt/acc-connector/app/

ssh ubuntu@32.197.105.164 \
  '/opt/acc-connector/venv/bin/pip install -r /opt/acc-connector/app/requirements.txt && sudo systemctl restart acc-connector'

curl -sf https://formabricks-stg.cctech.co.in/health
```

`.env` and `connector.db` on the server are **never** touched by deploy.

### Deploy flow

```
Push to staging-deployment
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

## 11. First deploy and verification

### Ordered checklist

- [ ] DNS A record `formabricks-stg.cctech.co.in` → `32.197.105.164`
- [ ] EC2 t3.small running, security group open on 80/443
- [ ] Sections 2–7 complete: symlink, venv, `.env`, `connector.db`, systemd, Nginx, Certbot
- [ ] APS and Databricks OAuth apps registered with Staging URLs
- [ ] `ENABLE_DEPLOY=true` and Staging secrets configured
- [ ] Push to `staging-deployment` → Actions green for test + deploy-staging

### Quick verification

| Check | Command / action | Expected |
|-------|------------------|----------|
| HTTPS | `curl -I https://formabricks-stg.cctech.co.in/` | HTTP/2 200 |
| Health | `curl -sf https://formabricks-stg.cctech.co.in/health` | `{"status":"ok"}` |
| Gunicorn | `sudo systemctl status acc-connector` | active (running) |
| ACC OAuth | Browser → Sign in Autodesk | Redirect to `/callback` without error |
| Databricks OAuth | Sign in Databricks | Redirect to `/databricks/callback` |
| Multi-user | Two testers, different ACC accounts | Each sees own hub/project state |

Share with QA: **https://formabricks-stg.cctech.co.in**

---

## 12. Troubleshooting

View logs:

```bash
sudo journalctl -u acc-connector -n 100 --no-pager
sudo tail -f /var/log/nginx/error.log
```

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `cd /opt/acc-connector/app`: No such file | Empty `/opt/acc-connector` or broken symlink | Run [Section 1](#1-check-server-state), then [2A](#2a-clone-directly-to-opt--most-common) |
| `mv: cannot stat '/tmp/repo'` | `/tmp` cleared after earlier clone | Use [Section 2A](#2a-clone-directly-to-opt--most-common) — clone as ubuntu, then `mv` to `/opt` |
| `git@github.com: Permission denied (publickey)` | Clone ran as `accconnector` using ubuntu's key, or deploy key not in GitHub | [Section 2A](#2a-clone-directly-to-opt--most-common) — clone as **ubuntu**, then `chown` |
| `Identity file ... github_deploy not accessible` | Wrong user reading ubuntu's private key | Clone as **ubuntu** (not `sudo -u accconnector`) |
| `cd opt`: No such file | Missing leading `/` | Use `cd /opt/acc-connector/app` |
| 502 Bad Gateway | Gunicorn not running | `sudo systemctl restart acc-connector` |
| OAuth redirect mismatch | URL typo in `.env` or provider console | Match hostname checklist exactly |
| SSH / ping timeout from PC | SG allows SSH only from old IP (`103.8.39.122/32`); ICMP blocked | [Section 0](#0-ssh-access-and-security-group-read-first) — update SSH rule to your current `/32` |
| Deploy SSH fails (GitHub Actions) | SG blocks port 22 from GitHub runner IPs | See [Section 0](#0-ssh-access-and-security-group-read-first); widen SSH only for deploy test |
| SQLite permission error | Wrong owner on `connector.db` | `sudo chown accconnector:accconnector /var/lib/acc-connector/connector.db` |
| Service start fails | Missing `.env` or bad `SECRET_KEY` | Check `journalctl`; fix `/opt/acc-connector/app/.env` |

If something breaks after deploy:

```bash
sudo systemctl restart acc-connector
sudo journalctl -u acc-connector -n 50
```

---

## 13. Cost estimate

Approximate USD/month for us-east-1 (June 2026). Enterprise discounts and region vary.

| Item | Approx. cost |
|------|--------------|
| 1× EC2 t3.small on-demand | ~$15/month |
| 30 GB gp3 EBS | ~$2.40/month |
| 1 Elastic IP (attached) | Free |
| Route 53 hosted zone (if new domain) | ~$0.50/month |
| 1 Secrets Manager secret | ~$0.40/month |
| **Total** | **~$18–25/month** |

Databricks compute (warehouse, pipelines) is billed separately.

Save money: stop the EC2 instance nights/weekends if testers are not using Staging (~65% savings on compute).

---

## 14. Quick start checklist

**Already done (typical):**

- [x] EC2 instance running; SSH access works
- [x] `accconnector` user and empty `/opt/acc-connector/` exist

**Do now (this guide):**

- [ ] Section 1 — run diagnostic; note which path (2A / 2B / 2C)
- [ ] Section 2 — clone (or move) + symlink → `cd /opt/acc-connector/app` works
- [ ] Section 3 — Python venv + `pip install`
- [ ] Section 4 — create `/opt/acc-connector/app/.env`
- [ ] Section 5 — `connector.db` symlink
- [ ] Section 6 — systemd + local curl 200
- [ ] Section 7 — Nginx + Certbot HTTPS
- [ ] Section 10 — GitHub `ENABLE_DEPLOY` + secrets
- [ ] Section 11 — push branch → verify OAuth + bootstrap

**When you add Dev / Production later:**

| Step | Action |
|------|--------|
| Infrastructure | Additional EC2 instances (same pattern) |
| Branches | `develop` → Dev, `staging-deployment` → Staging, `main` → Production |
| Secrets | Separate `DEV_*` / `PROD_*` GitHub secrets; separate `.env` per server |
| OAuth | Separate callback URLs per environment |
| Database | Separate `connector.db` per environment — never copy Production DB to Staging |

---

*Guide version: June 2026 — continuation from EC2 clone; Staging hostname `formabricks-stg.cctech.co.in`, branch `staging-deployment`.*
