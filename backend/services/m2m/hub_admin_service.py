"""
Purpose: Enforces the hub-admin-only access policy (FR-02 FR-01a, BR-12, BO-08) for every hub
the signed-in user can see, and decides what to do when the answer is unknowable.

The role probe cannot succeed before the hub admin whitelists our Client ID, so "unverified"
is a real third outcome alongside admin / not_admin. Policy (HUB_ADMIN_ENFORCEMENT):

  strict (default) — unverified counts as denied, but with its own reason so the UI shows
                     "finish the Custom Integration step" rather than "you are not an admin".
                     Correct because the product documentation makes whitelisting the Client ID
                     a prerequisite completed *before* first sign-in, so by the time anyone
                     reaches us the probe is answerable.
  provisional      — unverified hubs are shown so an admin who skipped the documented step can
                     still reach the whitelist screen; SSA provisioning stays blocked until the
                     probe turns conclusive. Use if onboarding order cannot be relied on.
  permissive       — local dev without account-admin rights; every visible hub passes.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from backend.clients.acc import admin_client
from backend.clients.acc.admin_client import AccountAdminUnavailable

logger = logging.getLogger(__name__)

ADMIN = 'admin'
NOT_ADMIN = 'not_admin'
UNVERIFIED = 'unverified'

ENFORCEMENT_PROVISIONAL = 'provisional'
ENFORCEMENT_STRICT = 'strict'
ENFORCEMENT_PERMISSIVE = 'permissive'

CACHE_TTL_SEC = int(os.getenv('HUB_ADMIN_CACHE_TTL_SEC', '900'))

_cache: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()


class AccessDenied(PermissionError):
    """The signed-in user is not a hub admin anywhere we can see."""

    def __init__(self, message: str, *, reason: str = 'not_hub_admin'):
        super().__init__(message)
        self.reason = reason


def enforcement_mode() -> str:
    mode = (os.getenv('HUB_ADMIN_ENFORCEMENT') or ENFORCEMENT_STRICT).strip().lower()
    if mode not in (ENFORCEMENT_PROVISIONAL, ENFORCEMENT_STRICT, ENFORCEMENT_PERMISSIVE):
        raise ValueError(f'unknown HUB_ADMIN_ENFORCEMENT: {mode!r}')
    return mode


def invalidate(aps_user_id: str | None = None) -> None:
    with _lock:
        if aps_user_id is None:
            _cache.clear()
        else:
            for key in [k for k in _cache if k[0] == aps_user_id]:
                _cache.pop(key, None)


def _cached(aps_user_id: str, hub_id: str) -> str | None:
    with _lock:
        entry = _cache.get((aps_user_id, hub_id))
        if entry and time.time() < entry['expires_at']:
            return entry['status']
    return None


def _remember(aps_user_id: str, hub_id: str, status: str) -> None:
    with _lock:
        _cache[(aps_user_id, hub_id)] = {
            'status': status,
            'expires_at': time.time() + CACHE_TTL_SEC,
        }


def check_hub_admin(aps_user_id: str, hub_id: str, *, client_id: str,
                    client_secret: str, use_cache: bool = True) -> str:
    """Return ADMIN, NOT_ADMIN or UNVERIFIED for one hub."""
    if enforcement_mode() == ENFORCEMENT_PERMISSIVE:
        return ADMIN

    if use_cache:
        cached = _cached(aps_user_id, hub_id)
        if cached is not None:
            return cached

    account_id = admin_client.account_id_for_hub(hub_id)
    try:
        token = admin_client.get_account_token(client_id, client_secret)
        status = ADMIN if admin_client.is_account_admin(
            token, account_id, aps_user_id,
        ) else NOT_ADMIN
    except AccountAdminUnavailable as exc:
        logger.info('Hub-admin probe inconclusive for %s on %s: %s',
                    aps_user_id, hub_id, exc)
        status = UNVERIFIED

    _remember(aps_user_id, hub_id, status)
    return status


def annotate_hubs(aps_user_id: str, hubs: list[dict], *, client_id: str,
                  client_secret: str) -> list[dict]:
    """Tag each visible hub with its admin status, dropping the ones we can definitively
    say the user does not administer."""
    out = []
    for hub in hubs:
        status = check_hub_admin(
            aps_user_id, hub['id'], client_id=client_id, client_secret=client_secret,
        )
        if status == NOT_ADMIN:
            continue
        entry = dict(hub)
        entry['admin_status'] = status
        out.append(entry)
    return out


def admin_hubs(aps_user_id: str, hubs: list[dict], *, client_id: str,
               client_secret: str) -> list[dict]:
    """Hubs this user may act on, or raise AccessDenied.

    Raising rather than returning an empty list keeps the two denial reasons distinct in the
    UI: a project admin gets "Access Restricted" (FR-06 §4.2), while an APS outage gets the
    retryable "couldn't verify" screen. Conflating them tells a real admin they are not one.
    """
    candidates = annotate_hubs(
        aps_user_id, hubs, client_id=client_id, client_secret=client_secret,
    )
    if candidates:
        if enforcement_mode() == ENFORCEMENT_STRICT:
            confirmed = [h for h in candidates if h['admin_status'] == ADMIN]
            if not confirmed:
                raise AccessDenied(
                    'Could not verify your hub administrator access.',
                    reason='unverified',
                )
            return confirmed
        return candidates

    if not hubs:
        raise AccessDenied(
            'This connector is available only to ACC / Forma Hub Administrators.',
            reason='no_hubs',
        )
    raise AccessDenied(
        'This connector is available only to ACC / Forma Hub Administrators.',
        reason='not_hub_admin',
    )


def require_conclusive_admin(aps_user_id: str, hub_id: str, *, client_id: str,
                             client_secret: str) -> None:
    """Gate for actions that consume a real resource — provisioning a robot above all.

    Once the hub is whitelisted the probe is conclusive, so this is where a non-admin who
    slipped through the provisional sign-in is stopped, before they can burn one of the ten
    service-account slots on the vendor's Client ID.
    """
    if enforcement_mode() == ENFORCEMENT_PERMISSIVE:
        return
    status = check_hub_admin(
        aps_user_id, hub_id, client_id=client_id, client_secret=client_secret,
        use_cache=False,
    )
    if status == ADMIN:
        return
    if status == NOT_ADMIN:
        raise AccessDenied(
            'Only a hub administrator can provision the service account for this hub.',
            reason='not_hub_admin',
        )
    raise AccessDenied(
        'Whitelist the connector Client ID in Custom Integrations first — until then we '
        'cannot confirm your hub administrator access.',
        reason='unverified',
    )
