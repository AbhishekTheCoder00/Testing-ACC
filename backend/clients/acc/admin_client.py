"""
Purpose: Answers "is this Autodesk user an account admin of this hub?" against the ACC/BIM 360
HQ Account Admin API (FR-02 FR-01a), which is the only API surface that exposes the role.

Two properties of that API drive the design: it needs a 2-legged token with account:read, and
it returns 403 until the hub admin has whitelisted our Client ID in Custom Integrations. So a
403 means "cannot tell yet", NOT "not an admin" — the two are reported separately and the
caller decides policy. See ADR.md (Phase 1) and hub_admin_service.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .constants import APS_TOKEN_URL

logger = logging.getLogger(__name__)

ACC_HQ_BASE = 'https://developer.api.autodesk.com/hq/v1'
ACCOUNT_ADMIN_ROLE = 'account_admin'
ACCOUNT_SCOPE = 'account:read'
# Data Management (`/project/v1/...`) refuses an `account:read` token and vice versa, so a
# caller probing that API must ask for this instead. Verified against a live hub: with a
# whitelisted Client ID, account:read → hq/v1 200 / project/v1 403, and data:read the reverse.
DATA_SCOPE = 'data:read'

_HTTP_TIMEOUT = 30
_PAGE_SIZE = 100
# An account with more members than this is possible; we stop rather than page forever.
# Not finding the user then reports "unverified", never a false "not an admin".
_MAX_PAGES = 20
_TOKEN_BUFFER_SEC = 120


class AccountAdminUnavailable(RuntimeError):
    """The role could not be determined — app not whitelisted, wrong region, or APS down.

    Deliberately distinct from a definitive "not an admin": denying a real hub admin during
    an Autodesk outage, or before they have had a chance to whitelist us, is a different
    product decision from refusing a project admin.
    """


_tokens: dict[str, dict] = {}
_lock = threading.Lock()


def invalidate_token_cache() -> None:
    with _lock:
        _tokens.clear()


def get_account_token(client_id: str, client_secret: str, *,
                      scope: str = ACCOUNT_SCOPE) -> str:
    """2-legged token for the given scope — the HQ endpoints reject 3-legged tokens.

    Cached per (client_id, scope): the scopes are not interchangeable, so caching on the
    client id alone would hand an `account:read` token to a Data Management caller and
    produce a 403 that looks exactly like "not whitelisted".
    """
    cache_key = (client_id, scope)
    with _lock:
        entry = _tokens.get(cache_key)
        if entry and time.time() < entry['expires_at'] - _TOKEN_BUFFER_SEC:
            return entry['token']

    try:
        resp = requests.post(
            APS_TOKEN_URL,
            auth=(client_id, client_secret),
            data={'grant_type': 'client_credentials', 'scope': scope},
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AccountAdminUnavailable(f'APS account token request failed: {exc}') from exc

    if not getattr(resp, 'ok', False):
        raise AccountAdminUnavailable(
            f'APS account token mint failed: {resp.status_code}'
        )
    payload = resp.json() or {}
    token = payload.get('access_token')
    if not token:
        raise AccountAdminUnavailable('APS account token response had no access_token')

    with _lock:
        _tokens[cache_key] = {
            'token': token,
            'expires_at': time.time() + int(payload.get('expires_in') or 3600),
        }
    return token


def account_id_for_hub(hub_id: str) -> str:
    """Data Management hub ids are the account UUID with a ``b.`` prefix (FR-01)."""
    return (hub_id or '').strip().removeprefix('b.')


def _get_page(token: str, account_id: str, offset: int) -> list[dict]:
    url = f'{ACC_HQ_BASE}/accounts/{account_id}/users'
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {token}'},
            params={'limit': _PAGE_SIZE, 'offset': offset},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AccountAdminUnavailable(f'ACC account users request failed: {exc}') from exc

    status = getattr(resp, 'status_code', 0)
    if status in (401, 403, 404):
        # 403 is overwhelmingly "Client ID not in this account's Custom Integrations".
        raise AccountAdminUnavailable(
            f'ACC account users returned {status} for account {account_id} — the vendor '
            'Client ID is probably not whitelisted for this hub yet'
        )
    if not getattr(resp, 'ok', False):
        raise AccountAdminUnavailable(
            f'ACC account users returned {status} for account {account_id}'
        )

    payload = resp.json()
    if isinstance(payload, list):
        return payload
    return (payload or {}).get('data', []) or []


def find_account_user(token: str, account_id: str, aps_user_id: str) -> dict | None:
    """Locate the signed-in Autodesk user in the account member list.

    Matching is on ``uid``, the Autodesk id — the ``id`` field is an ACC-internal user id
    and is not what OAuth gives us.
    """
    for page in range(_MAX_PAGES):
        rows = _get_page(token, account_id, offset=page * _PAGE_SIZE)
        for row in rows:
            if row.get('uid') == aps_user_id:
                return row
        if len(rows) < _PAGE_SIZE:
            return None
    raise AccountAdminUnavailable(
        f'account {account_id} has more members than this probe will page through'
    )


def is_account_admin(token: str, account_id: str, aps_user_id: str) -> bool:
    """True when the user carries the account_admin role on this account.

    Raises AccountAdminUnavailable when the answer cannot be determined. A user who is
    genuinely absent from the account is a definitive False.
    """
    user = find_account_user(token, account_id, aps_user_id)
    if user is None:
        return False
    return (user.get('role') or '').strip().lower() == ACCOUNT_ADMIN_ROLE
