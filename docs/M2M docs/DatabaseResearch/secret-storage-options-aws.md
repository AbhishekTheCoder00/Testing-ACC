# Secret Storage — Options A / B / C, with AWS, Cost, Security & Effort

> Companion to [`m2m-credentials-and-ssa-design.md`](./m2m-credentials-and-ssa-design.md).
> This one is hands-on: for each option it shows **what it is in plain words**, the
> **steps**, the **code**, **how it connects to AWS**, the **cost**, the **security level**,
> and — at the very end — the **effort** to build it.
>
> Written assuming you have **not** used AWS Secrets Manager, AWS KMS, or "envelope
> encryption" before. Read §0 first; it makes the rest obvious.

---

## 0. Two concepts in plain words (read this first)

### 0.1 The problem we are solving
A secret (like the SSA private key) has to be **encrypted when stored on disk**. To decrypt
it you need a **key**. The obvious question: *where do you keep that key?*

- Today we keep it (`SECRET_KEY`) **inside the same `admin.db` file** it protects. That's like
  locking a safe and taping the key to the safe door. Anyone who copies the file gets both.
- Every option below is really just a better answer to **"where does the key live?"**

### 0.2 What is "envelope encryption"? (the letter-in-an-envelope idea)
Instead of one key, you use **two**:

```
  Secret  ──encrypt with──▶  DEK (Data Encryption Key, random, per secret)
  DEK     ──encrypt with──▶  KEK (Key Encryption Key, kept somewhere very safe)

  Stored on disk:  [ encrypted secret ]  +  [ encrypted DEK ]
  Kept elsewhere:  the KEK  (never on disk next to the data)
```

- The DEK is the "letter". The KEK is the "envelope" that wraps it.
- To read a secret: get the KEK → decrypt the DEK → decrypt the secret.
- **Why bother?** The KEK can live in special hardware (AWS KMS) that *never reveals it* — it
  only decrypts small blobs for you. And rotating the KEK is cheap: you re-wrap the little DEK,
  you don't re-encrypt gigabytes of data.

### 0.3 What is AWS Secrets Manager? (a managed password vault)
A service where you **store secrets in AWS**, and your app **asks AWS for them at runtime**:

```
  app  ──"give me secret 'acc-connector/m2m'"──▶  AWS Secrets Manager  ──▶  the secret (over TLS)
```

AWS handles encryption-at-rest, who's allowed to read it (IAM), an audit log of every read
(CloudTrail), and optional automatic rotation. You never store the secret in your own DB.

### 0.4 What is AWS KMS? (a key safe you can't open)
Key Management Service holds keys **inside hardware you can never extract them from**. You
don't get the key; you ask KMS to *encrypt* or *decrypt* small things for you. It's what
powers the "KEK" in envelope encryption.

### 0.5 The one glue concept: the EC2 **instance role** (no passwords in code)
Our app runs on a Linux server (gunicorn + nginx + systemd — see
[`deploy/acc-connector.service`](../deploy/acc-connector.service)). On AWS that server is an
**EC2 instance**. You attach an **IAM role** ("instance profile") to it. Then:

```
  EC2 instance (has role "acc-connector-role")
        │  boto3 automatically reads temporary AWS credentials
        │  from the instance metadata — NO access keys in code, NO keys in .env
        ▼
  AWS Secrets Manager / KMS   (checks the role's IAM policy, then answers)
```

This is the AWS version of "the machine's own identity is its password." It is how **all three
AWS options below authenticate** — you set it up once.

> One-time AWS setup shared by Options A and B(KMS):
> 1. In IAM, create a role `acc-connector-role` (trusted by EC2).
> 2. Attach it to the EC2 instance (Actions → Security → Modify IAM role).
> 3. Give the role a small policy (shown per-option below).
> 4. `pip install boto3` and set `AWS_REGION` (e.g. `us-east-1`) in the systemd `.env`.
> For **local dev**, run `aws configure` once (or set `AWS_ACCESS_KEY_ID` /
> `AWS_SECRET_ACCESS_KEY` env vars) so boto3 has creds off-AWS.

---

## Baseline (where we are now)
`SECRET_KEY` and the encrypted secrets both live in `admin.db`. Works, but the key sits next
to the lock. Everything below moves the key somewhere safer. You can adopt them **incrementally**
— C now, B next, A when you go to production.

---

## Option C — private key as a file on disk *(simplest, standard for keys)*

### What it is
Keep the SSA PEM as a normal file with **`0600` permissions** (only the app's Linux user can
read it), outside the web root. Store only the *path* in config. This is the conventional way
to keep a private key and is what the code already expects
(`ACC_SSA_PRIVATE_KEY_PATH`).

### Steps
1. Put the PEM on the server, e.g. `/opt/acc-connector/secrets/acc_ssa_private.pem`.
2. Lock it down:
   ```bash
   sudo chown accconnector:accconnector /opt/acc-connector/secrets/acc_ssa_private.pem
   sudo chmod 600 /opt/acc-connector/secrets/acc_ssa_private.pem
   ```
3. Point config at the path (env var or the admin store).
4. If the admin **pastes** the PEM in the M2M tab, write it to that path with `0600` instead of
   storing it in the DB.

### Code (paste → file)
```python
import os

def save_ssa_key_to_file(pem_text: str, path: str) -> str:
    # Open with 0600 from the start so it is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(pem_text.strip() + '\n')
    return path
# _ssa_private_key() already does key_path.read_bytes() — no change needed to the read side.
```
> Windows note: `0600` is a POSIX concept. On the Linux deploy host it works; on a Windows dev
> box the permission bits are advisory — fine for dev, the real protection is on the server.

### How it connects to AWS
None required. It uses the EC2 instance's own encrypted disk (EBS). If you enable **EBS
encryption** on the volume (one checkbox at launch), the file is also encrypted at rest by AWS
under the hood — free.

### Cost
**$0.** (You already pay for the disk.)

### Security
Good for the key itself — this is the standard pattern. Weakness: anyone with root on the box,
or a stolen disk snapshot (if EBS isn't encrypted), can read it. No audit log of access.

### Use when
Right now, for the PEM specifically. It's the least work and already supported.

---

## Option B — envelope encryption, key kept OUTSIDE the DB

Two flavors. **B1** needs no AWS; **B2** uses AWS KMS and is stronger.

### B1 — master key in an env var / `0600` file *(no AWS, small upgrade)*

**What it is:** stop storing `SECRET_KEY` in `admin.db`. Put it on the host (systemd
`EnvironmentFile`), and keep only the *encrypted* secrets in `admin.db`. Now the DB file alone
is useless.

**Steps**
1. Generate the key once:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
2. Put it in a locked host file that systemd already loads
   ([`acc-connector.service`](../deploy/acc-connector.service) has
   `EnvironmentFile=/opt/acc-connector/app/.env`):
   ```bash
   echo "ACC_MASTER_KEY=<the-hex-from-step-1>" | sudo tee -a /opt/acc-connector/app/.env
   sudo chmod 600 /opt/acc-connector/app/.env
   sudo chown accconnector:accconnector /opt/acc-connector/app/.env
   ```
3. Change the master-key resolver to read the host, not the DB.

**Code** (in [`encryption.py`](../backend/repositories/state/encryption.py)):
```python
def _master_key() -> str:
    key = os.getenv('ACC_MASTER_KEY')        # from the 0600 host file, NOT admin.db
    if not key:
        raise RuntimeError('ACC_MASTER_KEY not set on the host')
    return key
# encrypt()/decrypt() already derive Fernet from this. admin.db now stores only ciphertext.
```
> Migration: decrypt existing rows with the old key once, re-encrypt with the new key, then
> drop `secret_key` from `admin.db`.

**AWS connection:** none. **Cost:** $0. **Security:** much better than baseline — the file that
holds the secrets no longer holds the key. Weakness: the key is still a plaintext string on the
host; a host compromise still exposes it (but a stolen `admin.db` alone does not).

### B2 — envelope encryption with AWS KMS *(strong, key never leaves AWS)*

**What it is:** the KEK is a KMS key you can never extract. For each secret, ask KMS for a
one-time DEK, encrypt the secret with the DEK, and store `[encrypted secret] + [encrypted DEK]`
in `admin.db`. To read, ask KMS to decrypt the DEK.

**Steps**
1. Create the KMS key:
   ```bash
   aws kms create-key --description "acc-connector master KEK"
   aws kms create-alias --alias-name alias/acc-connector --target-key-id <key-id>
   ```
2. Let the instance role use it — attach this IAM policy to `acc-connector-role`:
   ```json
   { "Version": "2012-10-17", "Statement": [{
       "Effect": "Allow",
       "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
       "Resource": "arn:aws:kms:us-east-1:<acct-id>:key/<key-id>"
   }]}
   ```
3. `pip install boto3`; set `AWS_REGION` and `ACC_KMS_KEY_ID=alias/acc-connector` in the host
   `.env`.

**Code**
```python
import base64, boto3, os
from cryptography.fernet import Fernet

_kms = boto3.client('kms', region_name=os.environ['AWS_REGION'])
_KEY = os.environ['ACC_KMS_KEY_ID']

def encrypt_secret(plaintext: str) -> dict:
    dk = _kms.generate_data_key(KeyId=_KEY, KeySpec='AES_256')   # AWS makes a fresh DEK
    token = Fernet(base64.urlsafe_b64encode(dk['Plaintext'])).encrypt(plaintext.encode())
    return {                                   # store BOTH of these in admin.db
        'ciphertext':  token.decode(),
        'wrapped_dek': base64.b64encode(dk['CiphertextBlob']).decode(),  # DEK encrypted by KMS
    }

def decrypt_secret(blob: dict) -> str:
    dek = _kms.decrypt(CiphertextBlob=base64.b64decode(blob['wrapped_dek']))['Plaintext']
    return Fernet(base64.urlsafe_b64encode(dek)).decrypt(blob['ciphertext'].encode()).decode()
```

**AWS connection:** boto3 → KMS, authenticated by the EC2 instance role (§0.5). The KEK stays
inside AWS hardware; only tiny DEK blobs travel.

**Cost:** **~$1 / month** per KMS key + **$0.03 per 10,000** KMS calls. With normal caching of
decrypted secrets in memory, calls are a handful per restart → effectively **~$1/month**.

**Security:** strong. A stolen `admin.db` is useless without KMS, and KMS access is IAM-gated and
logged in CloudTrail. The KEK can never be exfiltrated.

**Use when:** you want real at-rest protection but don't want to move secrets out of your app DB.

---

## Option A — AWS Secrets Manager *(managed vault, production standard)*

### What it is
Don't store the M2M secrets in `admin.db` at all. Keep them in AWS Secrets Manager and fetch
them at runtime. AWS does encryption, access control, audit, and (optionally) rotation.

### Steps
1. Create **one** secret holding all M2M values as JSON (one secret is cheaper than seven):
   ```bash
   aws secretsmanager create-secret --name acc-connector/m2m --secret-string '{
     "acc_m2m_client_id": "...",       "acc_m2m_client_secret": "...",
     "acc_ssa_user_id": "...",         "acc_ssa_key_id": "...",
     "acc_ssa_private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
     "databricks_m2m_client_id": "...","databricks_m2m_client_secret": "..."
   }'
   ```
2. Attach this policy to `acc-connector-role`:
   ```json
   { "Version": "2012-10-17", "Statement": [{
       "Effect": "Allow",
       "Action": ["secretsmanager:GetSecretValue"],
       "Resource": "arn:aws:secretsmanager:us-east-1:<acct-id>:secret:acc-connector/*"
   }]}
   ```
3. `pip install boto3`; set `AWS_REGION` in the host `.env`.

### Code
```python
import boto3, json, os
_sm = boto3.client('secretsmanager', region_name=os.environ['AWS_REGION'])
_cache = {}

def get_m2m_secrets() -> dict:
    if 'm2m' not in _cache:                       # cache so we don't call AWS every request
        resp = _sm.get_secret_value(SecretId='acc-connector/m2m')
        _cache['m2m'] = json.loads(resp['SecretString'])
    return _cache['m2m']

# m2m_service._acc_creds(), _ssa_settings(), _dbx_creds() read from get_m2m_secrets()
# instead of os.getenv(). The SSA private key comes straight from the JSON — no PEM file.
```

**Admin form choice:** either (a) the admin sets the secret **once via AWS console/CLI** (then
the M2M tab isn't needed for secrets), or (b) the M2M tab writes to Secrets Manager with an
extra `secretsmanager:PutSecretValue` permission. (a) is simpler and more standard.

### How it connects to AWS
boto3 → Secrets Manager, authenticated by the EC2 instance role (§0.5). Secrets never touch
`admin.db` or `.env`.

### Cost
**~$0.40 / month** for one secret + **$0.05 per 10,000** reads. With caching, reads are
negligible → **~$0.40–0.80/month**. (Uses the free `aws/secretsmanager` KMS key; a custom KMS
key would add ~$1/mo.)

### Security
Best of the three. Central rotation, per-secret IAM, full audit trail, nothing sensitive on
your host or in your DB. This is what an enterprise security review expects for production.

### Use when
Production, or as soon as you're comfortable with a small AWS dependency.

---

## Side-by-side comparison

| | C: file on disk | B1: key in host env | B2: envelope + KMS | A: Secrets Manager |
|---|---|---|---|---|
| Where the key/secret lives | PEM on EBS disk | master key in host file | KEK in AWS KMS | secret in AWS vault |
| Needs AWS? | No (EBS optional) | No | Yes (KMS + role) | Yes (SM + role) |
| Cost / month | $0 | $0 | ~$1 | ~$0.40–0.80 |
| Stolen `admin.db` alone is useless? | n/a (not in DB) | ✅ | ✅ | ✅ (not in DB) |
| Survives host compromise? | ❌ | ❌ | ⚠️ partial (needs live KMS access) | ⚠️ partial (needs live role) |
| Audit log of secret access | ❌ | ❌ | ✅ (CloudTrail) | ✅ (CloudTrail) |
| Built-in rotation | ❌ | ❌ | manual | ✅ |
| Setup effort | very low | low | medium | medium |

Security ranking: **A ≈ B2  >  B1  >  C  >  baseline (key-in-DB)**.
For the PEM specifically, **C is already respectable**; pair it with **B1/B2/A** for the DB-stored
secrets.

---

## Recommendation by phase

- **Pilot (now):** **C for the PEM** + **B1 for the master key** (move `SECRET_KEY` out of
  `admin.db` into the host `.env`). Zero AWS work, removes the worst weakness, ~half a day.
- **Hardening (next):** **B2 (KMS)** so a leaked DB/disk is worthless and access is audited.
- **Production:** **A (Secrets Manager)** for all M2M secrets; keep the PEM either in the same
  secret (A) or as a `0600` file (C). Then delete secrets from `.env`/`admin.db` entirely.

You can do them in that order without rework — each step builds on the last.

---

## Effort estimates (build work)

> Rough, one engineer, includes code + test on the EC2 host. Excludes obtaining the AWS
> account / IAM approvals, which may add lead time in a corporate environment.

| Option | Work | Effort |
|---|---|---|
| **C** (PEM file, paste→write `0600`) | Small helper + wire `_ssa_private_key`; already mostly supported | **~0.5 day** |
| **B1** (master key → host env) | New `_master_key` source, systemd `EnvironmentFile`, migrate + re-encrypt existing rows, drop `secret_key` column | **~0.5–1 day** |
| **B2** (envelope + KMS) | boto3 + KMS key + IAM policy, encrypt/decrypt with wrapped DEK, storage schema change, migration, EC2 test | **~2–3 days** |
| **A** (Secrets Manager) | Secret schema + IAM, boto3 fetch + cache, wire `m2m_service` accessors, optional admin write path, migration, remove DB/`.env` storage, EC2 test | **~2–4 days** |
| Cross-cutting (any AWS option) | One-time IAM role on EC2, boto3 install, `AWS_REGION`, local-dev creds, runbook | **~0.5 day** |

**Suggested first slice (best value/effort):** C + B1 together, **~1 day**, no AWS account
needed — this alone closes the "key sits next to the lock" gap. Move to B2/A when the AWS role
and approvals are in place.
```
