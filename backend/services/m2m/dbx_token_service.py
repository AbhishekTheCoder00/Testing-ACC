"""
Purpose: The only sanctioned way for the M2M path to get a Databricks token. Resolves the
connection's service principal, reads its secret from the SecretStore, mints via
client_credentials and caches the result per connection_id behind a lock (FR-03 §5.2, §10).
Also owns the reactive net from FR-03 §15.2: on a 401/403 from Databricks, force one re-mint
and retry the operation exactly once.
"""

from __future__ import annotations

import logging
import threading
import time

from backend.clients.dbx.m2m_auth_client import DbxAuthError, mint_workspace_token
from backend.repositories.state import connection_repository
from backend.secrets import get_secret_store

logger = logging.getLogger(__name__)

# Re-mint this long before the token actually expires, so a long-running pipeline poll does
# not hand a Databricks call a token that dies mid-flight.
TOKEN_MINT_BUFFER_SEC = 120

_cache: dict[str, dict] = {}
_lock = threading.Lock()


class MissingServicePrincipal(RuntimeError):
    """The connection has no Databricks service principal, so it cannot run headlessly."""


def invalidate(connection_id: str | None = None) -> None:
    """Drop cached tokens. Call after rotating an SP secret or changing the workspace."""
    with _lock:
        if connection_id is None:
            _cache.clear()
        else:
            _cache.pop(connection_id, None)


def get_dbx_token(connection_id: str, *, force: bool = False) -> str:
    """Return a valid workspace token for this connection, minting if needed."""
    if not force:
        with _lock:
            entry = _cache.get(connection_id)
            if entry and time.time() < entry['expires_at'] - TOKEN_MINT_BUFFER_SEC:
                return entry['token']

    creds = connection_repository.get_dbx_credentials(connection_id)
    if not creds:
        raise MissingServicePrincipal(
            f'connection {connection_id} has no Databricks service principal — '
            'scheduled sync requires one (add SP credentials in the setup wizard)'
        )

    secret = get_secret_store().get_secret(creds['client_secret_ref'])
    resp = mint_workspace_token(creds['workspace_url'], creds['client_id'], secret)

    with _lock:
        _cache[connection_id] = {
            'token': resp.access_token,
            'expires_at': time.time() + resp.expires_in,
        }
    return resp.access_token


def is_auth_error(exc: Exception) -> bool:
    """Whether a Databricks failure is worth re-minting for.

    Only 401/403. Retrying a 404 or a 500 with a fresh token just doubles the damage and
    hides the real fault.
    """
    if isinstance(exc, DbxAuthError):
        return True
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status in (401, 403):
        return True
    text = str(exc)
    return '401' in text or '403' in text


def with_auth_retry(connection_id: str, operation):
    """Run ``operation(token)``; on a 401/403 re-mint once and try again.

    Databricks tokens can be revoked or expire between the cache check and the call, and a
    long sync makes that window real. One retry only — a second failure is a real problem.
    """
    token = get_dbx_token(connection_id)
    try:
        return operation(token)
    except Exception as exc:
        if not is_auth_error(exc):
            raise
        logger.warning(
            'Databricks rejected the token for connection %s (%s); re-minting and '
            'retrying once', connection_id, exc,
        )
        fresh = get_dbx_token(connection_id, force=True)
        return operation(fresh)
