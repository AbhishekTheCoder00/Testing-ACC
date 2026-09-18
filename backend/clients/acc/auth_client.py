import time
import logging

from .constants import *
from .http_client import *




# import requests

from backend.config import get_config
from backend.repositories import state_store as db



logger = logging.getLogger(__name__)






















#######################################################
def _client_id() -> str:
    v = get_config('APS_CLIENT_ID', '')
    if not v:
        raise RuntimeError('APS_CLIENT_ID not configured — register it in the app')
    return v


def _client_secret() -> str:
    v = get_config('APS_CLIENT_SECRET', '')
    if not v:
        raise RuntimeError('APS_CLIENT_SECRET not configured — register it in the app')
    return v


def _redirect_uri() -> str:
    return get_config('APS_REDIRECT_URI', 'http://localhost:8000/callback')


# ---------------------------------------------------------------------------
# 2-legged token (client_credentials) — used for hub/project discovery
# Hub listing via 2-legged checks app provisioning (BIM360 Account Admin),
# not user BIM360 regional membership, so it works for all Forma hubs.
# ---------------------------------------------------------------------------

_2legged_cache: dict = {}


def _get_2legged_token() -> str:
    """Return a cached 2-legged (client_credentials) token with data:read scope."""
    cached = _2legged_cache.get('token')
    if cached and time.time() < _2legged_cache.get('expires_at', 0):
        return cached
    r = requests.post(
        f'{APS_BASE_AUTH}/token',
        data={
            'grant_type':    'client_credentials',
            'client_id':     _client_id(),
            'client_secret': _client_secret(),
            'scope':         'data:read',
        },
        timeout=15
    )
    r.raise_for_status()
    tok = r.json()
    _2legged_cache['token']      = tok['access_token']
    _2legged_cache['expires_at'] = time.time() + tok.get('expires_in', 3600) - 60
    logger.info('2-legged token refreshed, expires in %ds', tok.get('expires_in', 3600))
    return tok['access_token']


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def get_auth_url() -> str:
    """Return the Autodesk authorization URL to redirect the user to."""
    from urllib.parse import quote
    encoded_redirect = quote(_redirect_uri(), safe='')
    return (
        f'{APS_BASE_AUTH}/authorize'
        f'?client_id={_client_id()}'
        f'&response_type=code'
        f'&redirect_uri={encoded_redirect}'
        f'&scope=data%3Aread%20data%3Awrite%20data%3Acreate%20data%3Asearch%20account%3Aread%20account%3Awrite%20user%3Aread%20viewables%3Aread%20user-profile%3Aread'
    )


def fetch_userinfo(access_token: str) -> dict:
    """Fetch the authenticated APS user's profile.

    Returned dict includes at minimum ``userId`` (Autodesk's stable identifier),
    ``userName`` and ``emailId``. Used at OAuth callback time to derive a
    session-scoped ``user_id`` so the connector can serve multiple concurrent
    pilot users without colliding on a hardcoded id.
    """
    r = requests.get(
        f'{APS_BASE_PROFILE}/users/@me',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def handle_callback(code: str) -> str:
    """Exchange the authorization code, identify the user via APS userinfo,
    persist the tokens encrypted under that user's id, and return the
    Autodesk ``userId`` so the caller can bind it to its session.
    """
    r = requests.post(
        f'{APS_BASE_AUTH}/token',
        data={
            'grant_type':    'authorization_code',
            'code':           code,
            'client_id':      _client_id(),
            'client_secret':  _client_secret(),
            'redirect_uri':   _redirect_uri(),
        },
        timeout=15
    )
    r.raise_for_status()
    tokens = r.json()
    logger.info('ACC token granted scopes: %s', tokens.get('scope', 'NOT_RETURNED'))

    access_token  = tokens['access_token']
    refresh_token = tokens['refresh_token']
    expires_in    = tokens.get('expires_in', 3600)

    # Try to fetch the user profile using the freshly-minted 3-legged token.
    # If the call fails (some APS responses return 410/Gone for certain tokens),
    # attempt to extract a stable user identifier from returned tokens as a
    # fallback so the connector can continue to bind tokens to a user.
    try:
        profile = fetch_userinfo(access_token)
        user_id = profile.get('userId')
        if not user_id:
            raise RuntimeError('APS userinfo did not return a userId')
    except Exception as exc:
        # Try to recover a user id from the token payload (id_token or access_token).
        def _extract_userid_from_jwt(token: str) -> str | None:
            try:
                import json, base64
                parts = token.split('.')
                if len(parts) < 2:
                    return None
                payload_b64 = parts[1]
                # pad base64
                padding = '=' * (-len(payload_b64) % 4)
                payload_b64 += padding
                data = base64.urlsafe_b64decode(payload_b64.encode())
                claims = json.loads(data.decode())
                # Common claim names to try
                for key in ('userId', 'userid', 'sub', 'user_id', 'email'):
                    if key in claims:
                        return str(claims[key])
            except Exception:
                return None
            return None

        user_id = None
        # id_token sometimes present in the token response (OIDC). Try it first.
        id_token = tokens.get('id_token')
        if id_token:
            user_id = _extract_userid_from_jwt(id_token)
        if not user_id:
            user_id = _extract_userid_from_jwt(access_token)
        if not user_id:
            # Give a clear error that includes the original exception message
            raise RuntimeError(f'APS userinfo fetch failed: {exc}')

    db.save_acc_tokens(
        user_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
    # Try to log human-friendly identity if available
    try:
        display = profile.get('emailId') or profile.get('userName') or 'no-email'
    except Exception:
        display = 'no-email'
    logger.info(
        'ACC tokens saved for user %s (%s)',
        user_id,
        display,
    )
    return user_id


def get_valid_token(user_id: str, *, force_refresh: bool = False) -> str:
    """
    Return a valid ACC access token, silently refreshing if near expiry.
    Raises RuntimeError if the user has not connected ACC yet.
    """
    record = db.get_acc_tokens(user_id)
    if not record:
        raise RuntimeError('ACC not connected — user must complete OAuth first')

    if (
        not force_refresh
        and time.time() < record['expires_at'] - TOKEN_REFRESH_BUFFER_SEC
    ):
        return record['access_token']

    # Silent token refresh
    logger.info('Refreshing ACC access token for user %s', user_id)
    r = requests.post(
        f'{APS_BASE_AUTH}/token',
        data={
            'grant_type':    'refresh_token',
            'refresh_token':  record['refresh_token'],
            'client_id':      _client_id(),
            'client_secret':  _client_secret(),
        },
        timeout=15
    )
    if r.status_code == 400:
        logger.error(
            '[acc-auth] refresh failed user=%s status=400 body=%s — re-authenticate in connector UI',
            user_id,
            r.text[:500],
        )
        raise RuntimeError('ACC refresh token expired — user must re-authenticate')
    if not r.ok:
        logger.error(
            '[acc-auth] refresh failed user=%s status=%s body=%s',
            user_id,
            r.status_code,
            r.text[:500],
        )
    r.raise_for_status()
    new_tokens = r.json()
    new_refresh = new_tokens.get('refresh_token') or record['refresh_token']
    db.update_acc_access_token(
        user_id,
        access_token=new_tokens['access_token'],
        expires_in=new_tokens.get('expires_in', 3600),
        refresh_token=new_refresh,
    )
    logger.info('[acc-auth] refresh succeeded user=%s', user_id)
    return new_tokens['access_token']
