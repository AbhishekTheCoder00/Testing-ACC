
import time
from .database import _conn
from .encryption import encrypt, decrypt

# ---------------------------------------------------------------------------
# ACC tokens
# ---------------------------------------------------------------------------

def save_acc_tokens(user_id: str, access_token: str, refresh_token: str, expires_in: int) -> None:
    now = time.time()
    with _conn() as con:
        con.execute('''
            INSERT INTO acc_tokens (user_id, access_token, refresh_token, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token  = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at    = excluded.expires_at,
                updated_at    = excluded.updated_at
        ''', (user_id, encrypt(access_token), encrypt(refresh_token), now + expires_in, now))

def get_acc_tokens(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM acc_tokens WHERE user_id = ?', (user_id,)).fetchone()
    if not row:
        return None
    return {
        'access_token':  decrypt(row['access_token']),
        'refresh_token': decrypt(row['refresh_token']),
        'expires_at':    row['expires_at'],
    }

def update_acc_access_token(
    user_id: str,
    access_token: str,
    expires_in: int,
    refresh_token: str | None = None,
) -> None:
    """Persist a refreshed ACC access token; update refresh_token when APS rotates it."""
    now = time.time()
    with _conn() as con:
        if refresh_token:
            con.execute('''
                UPDATE acc_tokens
                SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = ?
                WHERE user_id = ?
            ''', (
                encrypt(access_token),
                encrypt(refresh_token),
                now + expires_in,
                now,
                user_id,
            ))
        else:
            con.execute('''
                UPDATE acc_tokens
                SET access_token = ?, expires_at = ?, updated_at = ?
                WHERE user_id = ?
            ''', (encrypt(access_token), now + expires_in, now, user_id))

# ---------------------------------------------------------------------------
# Databricks credentials
# ---------------------------------------------------------------------------

def save_dbx_credentials(user_id: str, workspace_url: str, pat: str) -> None:
    with _conn() as con:
        con.execute('''
            INSERT INTO dbx_credentials (user_id, workspace_url, pat_encrypted, saved_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                workspace_url = excluded.workspace_url,
                pat_encrypted = excluded.pat_encrypted,
                saved_at      = excluded.saved_at
        ''', (user_id, workspace_url, encrypt(pat), time.time()))


def get_dbx_credentials(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM dbx_credentials WHERE user_id = ?', (user_id,)).fetchone()
    if not row:
        return None
    return {
        'workspace_url': row['workspace_url'],
        'pat':           decrypt(row['pat_encrypted']),
    }


# ---------------------------------------------------------------------------
# Databricks OAuth tokens
# ---------------------------------------------------------------------------

def save_dbx_tokens(user_id: str, workspace_url: str, access_token: str,
                    refresh_token: str = None, expires_in: int = 3600,
                    cloud_provider: str = 'unknown') -> None:
    now = time.time()
    with _conn() as con:
        con.execute('''
            INSERT INTO dbx_tokens
                (user_id, workspace_url, cloud_provider, access_token, refresh_token, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                workspace_url  = excluded.workspace_url,
                cloud_provider = excluded.cloud_provider,
                access_token   = excluded.access_token,
                refresh_token  = excluded.refresh_token,
                expires_at     = excluded.expires_at,
                updated_at     = excluded.updated_at
        ''', (user_id, workspace_url, cloud_provider, encrypt(access_token),
              encrypt(refresh_token) if refresh_token else None,
              now + expires_in, now))


def get_dbx_tokens(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM dbx_tokens WHERE user_id = ?', (user_id,)).fetchone()
    if not row:
        return None
    return {
        'workspace_url':  row['workspace_url'],
        'cloud_provider': row['cloud_provider'],
        'access_token':   decrypt(row['access_token']),
        'refresh_token':  decrypt(row['refresh_token']) if row['refresh_token'] else None,
        'expires_at':     row['expires_at'],
    }


def update_dbx_access_token(user_id: str, access_token: str, expires_in: int = 3600) -> None:
    now = time.time()
    with _conn() as con:
        con.execute('''
            UPDATE dbx_tokens
            SET access_token = ?, expires_at = ?, updated_at = ?
            WHERE user_id = ?
        ''', (encrypt(access_token), now + expires_in, now, user_id))

