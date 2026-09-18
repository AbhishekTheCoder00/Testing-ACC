"""
Purpose: All Autodesk Secure Service Account HTTP lives here — creating the per-hub robot,
creating and rotating its RSA keys, and exchanging a signed JWT assertion for a 3LO-equivalent
access token (FR-03 §7, §9, §12.3). Kept as a pure client with no database or vault access so
the provisioning and token services above it stay testable, and so every APS SSA URL, the
assertion shape and the quota-error detection exist in exactly one place.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import jwt as pyjwt
import requests

from .constants import (
    ACC_SSA_SCOPES,
    APS_SSA_BASE,
    APS_TOKEN_URL,
    SSA_ASSERTION_TTL_SEC,
    SSA_CLOCK_SKEW_SEC,
    SSA_GRANT_TYPE,
    SSA_MGMT_SCOPES,
    SSA_ROBOT_NAME_PREFIX,
)

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30
_ADMIN_TOKEN_BUFFER_SEC = 120

# APS says "10 service accounts per Client ID"; the wire error is 400 cs-16 in some regions
# and 403 limit_exceeded in others, so match on either shape rather than one status code.
_QUOTA_MARKERS = ('cs-16', 'limit_exceeded', 'maximum of', 'quota')

_SECRET_PATTERNS = (
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
               re.DOTALL),
    re.compile(r'(?i)("?(?:assertion|client_secret|privateKey|access_token|refresh_token)"?'
               r'\s*[:=]\s*"?)([^",\s}]+)'),
    re.compile(r'eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
)


class SsaApiError(RuntimeError):
    """An APS SSA call failed."""


class SsaQuotaExceeded(SsaApiError):
    """The Client ID has hit its service-account limit — the hub cannot be provisioned."""


@dataclass(frozen=True)
class ServiceAccount:
    service_account_id: str
    email: str
    name: str = ''


@dataclass(frozen=True)
class ServiceAccountKey:
    kid: str
    private_key_pem: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    expires_in: int


# ---------------------------------------------------------------------------
# Logging safety
# ---------------------------------------------------------------------------


def _redact(body: str | None) -> str:
    """Strip key material, secrets and JWTs from a response body before logging it.

    Error bodies are genuinely useful for diagnosing a failed provision, but they can echo
    back the assertion we just sent. Redacting here means no caller has to remember to.
    """
    if not body:
        return ''
    out = str(body)
    out = _SECRET_PATTERNS[0].sub('[REDACTED KEY]', out)
    out = _SECRET_PATTERNS[1].sub(r'\1[REDACTED]', out)
    out = _SECRET_PATTERNS[2].sub('[REDACTED JWT]', out)
    return out[:1000]


def _raise_for_status(resp, action: str) -> None:
    if resp.ok:
        return
    body = _redact(getattr(resp, 'text', ''))
    lowered = body.lower()
    if resp.status_code in (400, 403, 429) and any(m in lowered for m in _QUOTA_MARKERS):
        raise SsaQuotaExceeded(
            f'{action} failed: APS service-account limit reached for this Client ID '
            f'({resp.status_code}) — {body}'
        )
    raise SsaApiError(f'{action} failed: {resp.status_code} — {body}')


# ---------------------------------------------------------------------------
# Phase B step 1 — 2-legged admin token
# ---------------------------------------------------------------------------

_admin_tokens: dict[str, dict] = {}
_admin_lock = threading.Lock()


def invalidate_admin_token_cache(client_id: str | None = None) -> None:
    with _admin_lock:
        if client_id is None:
            _admin_tokens.clear()
        else:
            _admin_tokens.pop(client_id, None)


def get_admin_token(client_id: str, client_secret: str) -> str:
    """client_credentials token with the SSA management scopes (FR-03 §7.1).

    Cached per Client ID: with sharding there is more than one vendor app in play, and a
    token minted for app_a must never be used to create a robot under app_b.
    """
    with _admin_lock:
        entry = _admin_tokens.get(client_id)
        if entry and time.time() < entry['expires_at'] - _ADMIN_TOKEN_BUFFER_SEC:
            return entry['token']

    resp = requests.post(
        APS_TOKEN_URL,
        # Basic auth only — FR-03 §7.1 warns against sending credentials both ways.
        auth=(client_id, client_secret),
        data={'grant_type': 'client_credentials', 'scope': SSA_MGMT_SCOPES},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=_HTTP_TIMEOUT,
    )
    _raise_for_status(resp, 'APS admin token')
    payload = resp.json()
    token = payload.get('access_token')
    if not token:
        raise SsaApiError('APS admin token response had no access_token')
    expires_in = int(payload.get('expires_in') or 3600)

    with _admin_lock:
        _admin_tokens[client_id] = {
            'token': token,
            'expires_at': time.time() + expires_in,
        }
    return token


# ---------------------------------------------------------------------------
# Phase B steps 2-3 — create the robot and its key
# ---------------------------------------------------------------------------


def robot_name_for_hub(hub_id: str) -> str:
    """Stable, unique-enough robot name. APS derives the invite email from it, so it ends
    up in front of the hub admin — keep it readable and tied to the hub."""
    slug = re.sub(r'[^a-z0-9]', '', (hub_id or '').lower().removeprefix('b.'))[:12]
    return f'{SSA_ROBOT_NAME_PREFIX}{slug or "hub"}'


def create_service_account(admin_token: str, *, hub_id: str, name: str | None = None) -> ServiceAccount:
    """POST /service-accounts (FR-03 §7.2). One robot per hub — callers must check first."""
    robot_name = name or robot_name_for_hub(hub_id)
    resp = requests.post(
        APS_SSA_BASE,
        headers={
            'Authorization': f'Bearer {admin_token}',
            'Content-Type': 'application/json',
        },
        json={'name': robot_name, 'firstName': 'Forma', 'lastName': 'Databricks Sync'},
        timeout=_HTTP_TIMEOUT,
    )
    _raise_for_status(resp, f'create service account for hub {hub_id}')
    payload = resp.json() or {}
    service_account_id = payload.get('serviceAccountId') or payload.get('serviceAccountID')
    email = payload.get('email')
    if not service_account_id or not email:
        raise SsaApiError(
            'APS create-service-account response was missing serviceAccountId or email'
        )
    logger.info('Provisioned SSA robot %s for hub %s', email, hub_id)
    return ServiceAccount(service_account_id=service_account_id, email=email, name=robot_name)


def create_key(admin_token: str, service_account_id: str) -> ServiceAccountKey:
    """POST /service-accounts/{id}/keys (FR-03 §7.3).

    The private key is returned exactly once. If it is not in the response there is nothing
    to store and the robot would be unusable, so that is an error rather than a warning.
    """
    resp = requests.post(
        f'{APS_SSA_BASE}/{service_account_id}/keys',
        headers={'Authorization': f'Bearer {admin_token}'},
        timeout=_HTTP_TIMEOUT,
    )
    _raise_for_status(resp, f'create key for service account {service_account_id}')
    payload = resp.json() or {}
    kid = payload.get('kid') or payload.get('keyId')
    private_key = payload.get('privateKey') or payload.get('private_key')
    if not kid or not private_key:
        raise SsaApiError(
            'APS create-key response was missing kid or privateKey — the key is shown '
            'once, so this robot now has an unusable key and needs a rotation'
        )
    return ServiceAccountKey(kid=kid, private_key_pem=private_key)


def list_keys(admin_token: str, service_account_id: str) -> list[dict]:
    resp = requests.get(
        f'{APS_SSA_BASE}/{service_account_id}/keys',
        headers={'Authorization': f'Bearer {admin_token}'},
        timeout=_HTTP_TIMEOUT,
    )
    _raise_for_status(resp, f'list keys for service account {service_account_id}')
    payload = resp.json()
    if isinstance(payload, list):
        return payload
    return (payload or {}).get('keys', [])


def delete_key(admin_token: str, service_account_id: str, key_id: str) -> None:
    """Remove a superseded key. A 404 is fine — rotation is meant to be re-runnable."""
    resp = requests.delete(
        f'{APS_SSA_BASE}/{service_account_id}/keys/{key_id}',
        headers={'Authorization': f'Bearer {admin_token}'},
        timeout=_HTTP_TIMEOUT,
    )
    if getattr(resp, 'status_code', 200) == 404:
        logger.info('SSA key %s already absent on %s', key_id, service_account_id)
        return
    _raise_for_status(resp, f'delete key {key_id}')


# ---------------------------------------------------------------------------
# Phase D — mint an ACC access token from the hub's private key
# ---------------------------------------------------------------------------


def build_assertion(
    *,
    client_id: str,
    service_account_id: str,
    key_id: str,
    private_key_pem: str,
    scopes: str = ACC_SSA_SCOPES,
) -> str:
    """Sign the JWT assertion APS exchanges for an access token (FR-03 §9.1)."""
    iat = int(time.time()) - SSA_CLOCK_SKEW_SEC
    return pyjwt.encode(
        {
            'iss': client_id,            # the vendor app that owns the robot
            'sub': service_account_id,   # the robot itself
            'aud': APS_TOKEN_URL,
            'iat': iat,
            'exp': iat + SSA_ASSERTION_TTL_SEC,
            'scope': scopes.split(),
        },
        private_key_pem,
        algorithm='RS256',
        headers={'kid': key_id, 'alg': 'RS256'},
    )


def exchange_jwt(
    *,
    client_id: str,
    client_secret: str,
    service_account_id: str,
    key_id: str,
    private_key_pem: str,
    scopes: str = ACC_SSA_SCOPES,
) -> TokenResponse:
    """Trade the assertion for an ~1h access token (FR-03 §9.2).

    There is no refresh token by design: the private key *is* the refresh mechanism, so an
    expired token is re-minted rather than refreshed.
    """
    assertion = build_assertion(
        client_id=client_id,
        service_account_id=service_account_id,
        key_id=key_id,
        private_key_pem=private_key_pem,
        scopes=scopes,
    )
    resp = requests.post(
        APS_TOKEN_URL,
        auth=(client_id, client_secret),
        data={'grant_type': SSA_GRANT_TYPE, 'assertion': assertion, 'scope': scopes},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=_HTTP_TIMEOUT,
    )
    _raise_for_status(resp, f'SSA token exchange for {service_account_id}')
    payload = resp.json() or {}
    access_token = payload.get('access_token')
    if not access_token:
        raise SsaApiError('SSA token exchange returned no access_token')
    return TokenResponse(
        access_token=access_token,
        expires_in=int(payload.get('expires_in') or 3600),
    )
