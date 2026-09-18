"""
Purpose: Session and authorisation for the multi-tenant portal (FR-02 FR-02/FR-03, NFR-02,
FR-03 §13). The session gains `tenant_id` (the hub being acted on) and `connection_id` on top
of the existing `user_id`.

Deliberately a separate module from utils/auth.py: that one is the U2M path's and must keep
working untouched. Every portal route wraps itself in `require_tenant` or `require_connection`,
so cross-tenant access is refused by default rather than by each route remembering to filter.
"""

from __future__ import annotations

import functools
import logging

from flask import jsonify, session

from backend.repositories.state import connection_repository, tenant_repository

logger = logging.getLogger(__name__)

SESSION_USER = 'user_id'
SESSION_TENANT = 'tenant_id'
SESSION_CONNECTION = 'connection_id'


class AuthError(Exception):
    """Carries the HTTP status a route should return."""

    def __init__(self, status: int, message: str, *, reason: str = ''):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason


def current_user_id() -> str | None:
    return session.get(SESSION_USER)


def current_tenant_id() -> str | None:
    return session.get(SESSION_TENANT)


def require_user() -> str:
    user_id = current_user_id()
    if not user_id:
        raise AuthError(401, 'Not signed in.', reason='not_authenticated')
    return user_id


def require_active_hub() -> tuple[str, str]:
    """Return (user_id, hub_id) for a hub the session has selected.

    Used by onboarding, which runs *before* the tenant row exists — the tenant is created by
    ensure_ssa, so demanding a membership row here would lock a brand-new hub out of its own
    setup. The hub-admin check already ran when the hub was selected.
    """
    user_id = require_user()
    hub_id = current_tenant_id()
    if not hub_id:
        raise AuthError(400, 'No hub selected.', reason='no_hub')
    return user_id, hub_id


def require_tenant() -> tuple[str, str]:
    """As require_active_hub, but the user must also be mapped to an existing tenant.

    Membership is re-read every request rather than trusted from the cookie, so removing
    someone from a hub takes effect on their next request instead of whenever their session
    happens to expire.
    """
    user_id, hub_id = require_active_hub()
    if not tenant_repository.is_member(hub_id, user_id):
        logger.warning('User %s attempted to act on hub %s without a mapping', user_id, hub_id)
        raise AuthError(403, 'You do not administer this hub.', reason='not_a_member')
    return user_id, hub_id


def require_hub(hub_id: str) -> str:
    """Authorise an explicit hub in a URL path against the session (FR-03 §13).

    Uses the active-hub check, not the membership one: onboarding routes run before the tenant
    row exists, and the hub-admin check already happened when the hub was selected.
    """
    user_id, active = require_active_hub()
    if hub_id != active:
        raise AuthError(403, 'That hub is not the active hub for this session.',
                        reason='hub_mismatch')
    return user_id


def require_connection(connection_id: str) -> dict:
    """Load a connection, refusing one that belongs to another hub (TC-AUTH-05)."""
    _, hub_id = require_tenant()
    connection = connection_repository.get(connection_id)
    if not connection or connection['hub_id'] != hub_id:
        # Same response whether it is missing or someone else's — do not confirm existence.
        raise AuthError(404, 'Connection not found.', reason='not_found')
    return connection


def set_active_hub(hub_id: str) -> None:
    """Switch hub context. Clears the connection so nothing leaks across the switch."""
    session[SESSION_TENANT] = hub_id
    session.pop(SESSION_CONNECTION, None)


def clear_tenant_context() -> None:
    session.pop(SESSION_TENANT, None)
    session.pop(SESSION_CONNECTION, None)


def tenant_route(fn):
    """Wrap a portal route so AuthError becomes a JSON response instead of a traceback."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthError as exc:
            return jsonify({'error': exc.message, 'reason': exc.reason}), exc.status

    return wrapper
