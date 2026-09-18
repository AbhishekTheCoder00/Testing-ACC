"""
secret_repository.py — persistence for per-user app registration credentials.

Wide table keyed by ACC user_id (``app_secrets``). One row holds a single
ACC user's Databricks OAuth app credentials as columns, so each user enters and
uses their own credential set.

Only the sensitive ``DATABRICKS_CLIENT_SECRET`` is encrypted at write time with
the app Fernet key and decrypted at read time. The workspace URL and client id
are non-secret identifiers and are stored in plaintext.
"""
import time

from .database import _conn
from .encryption import encrypt, decrypt

SECRET_COLUMNS = (
    'DATABRICKS_WORKSPACE_URL',
    'DATABRICKS_CLIENT_ID',
    'DATABRICKS_CLIENT_SECRET',
)

# Columns stored encrypted at rest. Everything else in SECRET_COLUMNS is
# non-sensitive and stays plaintext.
SECRET_ENCRYPTED = {
    'DATABRICKS_CLIENT_SECRET',
}


def _invalidate() -> None:
    try:
        from backend.config import invalidate_config_cache
        invalidate_config_cache()
    except Exception:
        pass


def save_credentials(user_id: str, values: dict) -> None:
    """Upsert one ACC user's credential row (user_id-keyed).

    Only SECRET_ENCRYPTED columns are encrypted; the rest are stored plaintext.
    """
    user_id = (user_id or '').strip()
    now = time.time()
    placed = {}
    for c in SECRET_COLUMNS:
        v = values.get(c) or ''
        placed[c] = encrypt(v) if (c in SECRET_ENCRYPTED and v) else v
    cols = ', '.join(SECRET_COLUMNS)
    ph = ', '.join(['?'] * len(SECRET_COLUMNS))
    with _conn() as con:
        con.execute(f'''
            INSERT INTO app_secrets (user_id, {cols}, updated_at)
            VALUES (?, {ph}, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                DATABRICKS_WORKSPACE_URL = excluded.DATABRICKS_WORKSPACE_URL,
                DATABRICKS_CLIENT_ID     = excluded.DATABRICKS_CLIENT_ID,
                DATABRICKS_CLIENT_SECRET = excluded.DATABRICKS_CLIENT_SECRET,
                updated_at               = excluded.updated_at
        ''', (user_id, *[placed[c] for c in SECRET_COLUMNS], now))
    _invalidate()


def _decrypt_or_pass(value: str | None) -> str | None:
    """Decrypt a stored value, tolerating legacy plaintext rows.

    Older deployments wrote plaintext into app_secrets. Fernet raises
    ``InvalidToken`` when given non-ciphertext, so on failure we return the
    raw value unchanged — keeps existing rows readable until re-saved.
    """
    if not value:
        return value
    try:
        return decrypt(value)
    except Exception:
        return value


def _row_to_creds(row) -> dict:
    """Normalise a raw DB row (whose column casing differs between backends —
    SQLite preserves case, Postgres folds unquoted identifiers to lowercase)
    into a name -> value dict using the canonical SECRET_COLUMNS keys.

    Only SECRET_ENCRYPTED columns are decrypted here; non-sensitive columns are
    returned as stored. Callers (``get_config`` and the OAuth token exchange)
    always receive the plaintext secret.
    """
    raw = dict(row)
    out = {c: raw.get(c) for c in SECRET_COLUMNS}
    for c in SECRET_COLUMNS:
        if out[c] is None:
            out[c] = raw.get(c.lower())
    return {
        c: (_decrypt_or_pass(out[c]) if c in SECRET_ENCRYPTED else out[c])
        for c in SECRET_COLUMNS
    }


def encrypt_existing_plaintext() -> None:
    """Normalise legacy app_secrets rows to the current encryption policy.

    For SECRET_ENCRYPTED columns: plaintext -> ciphertext (legacy pre-encryption
    rows), ciphertext stays. For non-encrypted columns: ciphertext (from an
    earlier all-columns encryption policy) -> plaintext, plaintext stays.

    Idempotent and self-healing; called from ``init_db()`` on every startup.
    """
    with _conn() as con:
        rows = con.execute('SELECT * FROM app_secrets').fetchall()
    if not rows:
        return
    for row in rows:
        user_id = row['user_id']
        updates = {}
        now = time.time()
        for c in SECRET_COLUMNS:
            raw = row[c] if c in row.keys() else row.get(c.lower())
            if not raw:
                continue
            if c in SECRET_ENCRYPTED:
                # Plaintext -> encrypt. Ciphertext decrypts cleanly -> skip.
                try:
                    decrypt(raw)
                    continue
                except Exception:
                    pass
                updates[c] = encrypt(raw)
            else:
                # Ciphertext (old policy) -> revert to plaintext.
                try:
                    updates[c] = decrypt(raw)
                except Exception:
                    pass  # already plaintext
        if not updates:
            continue
        assignments = ', '.join(f'{c} = ?' for c in updates) + ', updated_at = ?'
        vals = [updates[c] for c in updates] + [now, user_id]
        with _conn() as con:
            con.execute(
                f'UPDATE app_secrets SET {assignments} WHERE user_id = ?', vals,
            )
    _invalidate()


def get_credentials(user_id: str) -> dict | None:
    """Return one ACC user's credential row as a name -> value dict, or None."""
    user_id = (user_id or '').strip()
    if not user_id:
        return None
    with _conn() as con:
        row = con.execute(
            'SELECT * FROM app_secrets WHERE user_id = ?', (user_id,)
        ).fetchone()
    return _row_to_creds(row) if row else None


def get_secret(user_id: str, name: str) -> str | None:
    """Return a single credential column for an ACC user, or None."""
    creds = get_credentials(user_id)
    if not creds:
        return None
    v = creds.get(name)
    return v if v else None


def has_credentials(user_id: str) -> bool:
    """True when the given ACC user has at least one stored credential row."""
    user_id = (user_id or '').strip()
    if not user_id:
        return False
    with _conn() as con:
        row = con.execute(
            'SELECT 1 AS one FROM app_secrets WHERE user_id = ? LIMIT 1', (user_id,)
        ).fetchone()
    return row is not None


def clear_credentials(user_id: str) -> None:
    """Delete one ACC user's credential row (and drop the config cache)."""
    user_id = (user_id or '').strip()
    with _conn() as con:
        con.execute('DELETE FROM app_secrets WHERE user_id = ?', (user_id,))
    _invalidate()