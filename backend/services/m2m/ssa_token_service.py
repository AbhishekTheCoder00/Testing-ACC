"""
Purpose: The only way the M2M path gets an ACC access token. Mints one per hub from that hub's
robot private key via JWT-bearer and caches it in-process (FR-03 §5.2, §9). This is what makes
scheduled sync independent of any human: there is no refresh token to expire — an expired token
is re-minted from the key, which is the whole point of the SSA model (FR-02 FR-04, BR-08).

`token_getter(hub_id)` returns a callable with the exact `get_token(refresh=False)` shape that
`clients/acc/data_connector_client._dc_get` already expects, so the existing force-refresh-on-401
retry works for the M2M path without editing a single client.
"""

from __future__ import annotations

import logging
import threading
import time

from backend.clients.acc import ssa_client
from backend.repositories.state import ssa_repository
from backend.secrets import get_secret_store
from backend.services.m2m import aps_app_service

logger = logging.getLogger(__name__)

# Re-mint this far ahead of expiry. A Data Connector export can run for many minutes, so
# handing out a token that dies mid-poll is a real failure mode rather than a theoretical one.
TOKEN_MINT_BUFFER_SEC = 120

_cache: dict[str, dict] = {}
_lock = threading.Lock()


class MissingServiceAccount(RuntimeError):
    """The hub has no SSA robot, so no ACC token can be minted for it."""


def invalidate(hub_id: str | None = None) -> None:
    """Drop cached tokens. Call after key rotation, SSA disable or a hub reset."""
    with _lock:
        if hub_id is None:
            _cache.clear()
        else:
            _cache.pop(hub_id, None)


def get_acc_token(hub_id: str, *, force: bool = False) -> str:
    """A valid ACC access token for this hub's robot, minting if needed."""
    if not force:
        with _lock:
            entry = _cache.get(hub_id)
            if entry and time.time() < entry['expires_at'] - TOKEN_MINT_BUFFER_SEC:
                return entry['token']

    credential = ssa_repository.get_by_hub_id(hub_id)
    if not credential:
        raise MissingServiceAccount(
            f'hub {hub_id} has no service account — provision one before syncing'
        )

    # Always the app recorded on the tenant: the robot's key is only valid under the Client ID
    # it was created with, so falling back to "the default app" would mint nothing usable.
    app = aps_app_service.get_app_for_hub(hub_id)
    store = get_secret_store()
    private_key = store.get_secret(credential['private_key_ref'])

    response = ssa_client.exchange_jwt(
        client_id=app['client_id'],
        client_secret=aps_app_service.get_client_secret(app),
        service_account_id=credential['service_account_id'],
        key_id=credential['key_id'],
        private_key_pem=private_key,
    )

    with _lock:
        _cache[hub_id] = {
            'token': response.access_token,
            'expires_at': time.time() + response.expires_in,
        }
    return response.access_token


def token_getter(hub_id: str):
    """A TokenGetter for this hub, shaped for the existing ACC clients.

    `data_connector_client` calls `get_token()` normally and `get_token(refresh=True)` after a
    401, so passing this in gives the M2M path the same automatic recovery the U2M path has.
    """

    def get_token(refresh: bool = False) -> str:
        return get_acc_token(hub_id, force=refresh)

    return get_token
