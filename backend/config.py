"""
config.py — unified app configuration accessor.

Priority (per the chosen model):
  1. Environment variable wins (local dev / tests / deploy override).
2. Otherwise fall back to the active user_id's row in the app_secrets table
      (the ACC user_id stored in the Flask session at sign-in time).

SECRET_KEY is intentionally NOT stored in the DB — it is the root key that
encrypts stored OAuth tokens, so it must live outside the database.
"""
import functools
import os

from backend.repositories import state_store as db


def _session_user_id() -> str | None:
    """Return the ACC user_id for the current session, if any."""
    try:
        from flask import session
        uid = session.get('user_id')
        return (uid or '').strip() or None
    except Exception:
        return None


@functools.lru_cache(maxsize=256)
def _db_secret(user_id: str, name: str) -> str | None:
    return db.get_secret(user_id, name)


def get_config(name: str, default: str = '', user_id: str | None = None) -> str:
    """Return a config value: env first, then the active user_id's DB row.

    ``user_id`` is optional — when omitted it is resolved from the Flask
    session's ``user_id`` (set at ACC sign-in time).
    """
    v = os.getenv(name)
    if v:
        return v
    resolved = user_id or _session_user_id()
    if not resolved:
        return default
    try:
        v = _db_secret(resolved, name)
        if v is not None:
            return v
    except Exception:
        pass
    return default


def invalidate_config_cache() -> None:
    """Drop the cached DB lookups (called after saving a secret)."""
    _db_secret.cache_clear()


def get_secret_key() -> str:
    """The root signing/encryption key. Always from env, never from the DB."""
    v = os.getenv('SECRET_KEY', '').strip()
    if not v:
        raise RuntimeError('SECRET_KEY environment variable is not set')
    return v