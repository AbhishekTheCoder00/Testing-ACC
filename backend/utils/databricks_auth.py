
import logging
import os
import threading
import time

import requests

from backend.config import get_config
from backend.repositories import state_store as db

logger = logging.getLogger(__name__)

DBX_OAUTH_SCOPE = os.getenv('DATABRICKS_OAUTH_SCOPE', 'all-apis offline_access')
TOKEN_REFRESH_BUFFER_SEC = 60   # rotate if token expires within 60 seconds

_oidc_endpoint_cache: dict[str, tuple[str, str]] = {}

# Databricks rotates refresh tokens on every use (single-use). If two threads
# refresh concurrently they both read the same token; the first rotates it and
# the second gets 403 with a now-dead token. Serialize refreshes per user and
# re-check inside the lock so the second thread reuses the freshly saved token.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _get_refresh_lock(user_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        lock = _refresh_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[user_id] = lock
        return lock


def _detect_cloud_provider(workspace_url: str) -> str:
    """AWS-only deployment — recognizes AWS Databricks workspace hostnames."""
    if 'cloud.databricks.com' in workspace_url or 'databricks.us' in workspace_url:
        return 'aws'
    return 'unknown'


def _resolve_databricks_oidc_endpoints(workspace_url: str) -> tuple[str, str]:
    """Return (authorize_url, token_url) for the given workspace, with caching."""
    base = workspace_url.rstrip('/')
    if base in _oidc_endpoint_cache:
        return _oidc_endpoint_cache[base]

    auth_override = os.getenv('DATABRICKS_OAUTH_AUTHORIZE_URL', '')
    token_override = os.getenv('DATABRICKS_OAUTH_TOKEN_URL', '')
    if auth_override and token_override:
        _oidc_endpoint_cache[base] = (auth_override, token_override)
        return auth_override, token_override

    try:
        meta = requests.get(
            f'{base}/oidc/.well-known/oauth-authorization-server',
            timeout=10,
        )
        if meta.ok:
            d = meta.json()
            auth_ep = d.get('authorization_endpoint', f'{base}/oidc/v1/authorize')
            token_ep = d.get('token_endpoint', f'{base}/oidc/v1/token')
            _oidc_endpoint_cache[base] = (auth_ep, token_ep)
            return auth_ep, token_ep
    except Exception as exc:
        logger.warning('OIDC discovery failed for %s: %s — using fallback', base, exc)

    auth_ep = f'{base}/oidc/v1/authorize'
    token_ep = f'{base}/oidc/v1/token'
    _oidc_endpoint_cache[base] = (auth_ep, token_ep)
    return auth_ep, token_ep


def _databricks_oauth_client_id(user_id: str) -> str:
    return get_config('DATABRICKS_CLIENT_ID', '', user_id=user_id).strip()


def _databricks_oauth_client_secret(user_id: str) -> str:
    return get_config('DATABRICKS_CLIENT_SECRET', '', user_id=user_id).strip()


def get_valid_dbx_token(user_id: str) -> dict:
    """
    Return Databricks OAuth token record, silently refreshing if near expiry.

    Persists both access_token and refresh_token after refresh to support
    Databricks single-use refresh token rotation.

    Raises RuntimeError if the user has not connected Databricks or refresh fails.
    """
    record = db.get_dbx_tokens(user_id)
    if not record:
        raise RuntimeError('Databricks not connected — complete OAuth first')

    if time.time() < record['expires_at'] - TOKEN_REFRESH_BUFFER_SEC:
        return record

    # Only one thread per user may refresh at a time — the single-use refresh
    # token would otherwise be consumed twice, failing the loser with a 403.
    with _get_refresh_lock(user_id):
        # Re-read inside the lock: another thread may have just refreshed while
        # we waited, in which case its freshly rotated token is now valid.
        record = db.get_dbx_tokens(user_id)
        if not record:
            raise RuntimeError('Databricks not connected — complete OAuth first')
        if time.time() < record['expires_at'] - TOKEN_REFRESH_BUFFER_SEC:
            return record

        return _do_refresh(user_id, record)


def _do_refresh(user_id: str, record: dict) -> dict:
    """Exchange the stored refresh token for a new access/refresh token pair.

    Caller must hold the per-user refresh lock.
    """
    refresh_token = record.get('refresh_token')
    if not refresh_token:
        raise RuntimeError(
            'Databricks refresh token missing — user must re-authenticate'
        )

    client_id = _databricks_oauth_client_id(user_id)
    client_secret = _databricks_oauth_client_secret(user_id)
    if not client_id or not client_secret:
        raise RuntimeError(
            'DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET must be set'
        )

    _, token_url = _resolve_databricks_oidc_endpoints(record['workspace_url'])
    logger.info('Refreshing Databricks access token for user %s', user_id)
    resp = requests.post(
        token_url,
        data={
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
            'client_id':     client_id,
            'client_secret': client_secret,
        },
        timeout=15,
    )
    if resp.status_code in (400, 401, 403):
        logger.error(
            '[dbx-auth] refresh failed user=%s workspace=%s status=%s body=%s — re-authenticate',
            user_id,
            record.get('workspace_url', ''),
            resp.status_code,
            resp.text[:500],
        )
        raise RuntimeError(
            'Databricks refresh token expired or revoked — user must re-authenticate'
        )
    resp.raise_for_status()
    tokens = resp.json()
    new_refresh = tokens.get('refresh_token') or refresh_token
    db.save_dbx_tokens(
        user_id,
        record['workspace_url'],
        tokens['access_token'],
        new_refresh,
        tokens.get('expires_in', 3600),
        cloud_provider=record.get('cloud_provider', 'unknown'),
    )
    logger.info('[dbx-auth] refresh succeeded user=%s workspace=%s', user_id, record['workspace_url'])
    return db.get_dbx_tokens(user_id)


def create_databricks_client_for_user(user_id: str):
    """Build a DatabricksClient with a refreshed U2M access token."""
    from backend.clients.databricks_client import DatabricksClient

    record = get_valid_dbx_token(user_id)
    return DatabricksClient(record['workspace_url'], record['access_token'])


def apply_valid_dbx_token_to_client(user_id: str, dbx) -> None:
    """Refresh the U2M OAuth token if needed and apply it to an existing client.

    Use as ``before_poll`` during long workflow / pipeline waits so polling
    survives access-token expiry (~1 hour) without forcing re-login.
    """
    record = get_valid_dbx_token(user_id)
    dbx.update_token(record['access_token'])
