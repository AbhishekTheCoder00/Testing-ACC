"""
Purpose: Mints a Databricks workspace token for a service principal via OAuth
client_credentials (FR-03 §10.2), which is how scheduled sync reaches Databricks without a
human. Deliberately tiny and dependency-free — the databricks-sdk would duplicate
clients/databricks_client.py for the sake of these twenty lines. There is no refresh-token
grant on this path: an expired token is re-minted from the SP secret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30
DBX_M2M_SCOPE = 'all-apis'


class DbxAuthError(RuntimeError):
    """Could not mint a Databricks service-principal token."""


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    expires_in: int


def token_endpoint(workspace_url: str) -> str:
    return f'{(workspace_url or "").strip().rstrip("/")}/oidc/v1/token'


def mint_workspace_token(
    workspace_url: str,
    client_id: str,
    client_secret: str,
) -> TokenResponse:
    """POST /oidc/v1/token with client_credentials (FR-03 §10.2).

    The secret is passed as HTTP Basic and never appears in a log line or an exception —
    error bodies from Databricks can echo request parameters back.
    """
    url = token_endpoint(workspace_url)
    try:
        resp = requests.post(
            url,
            auth=(client_id, client_secret),
            data={'grant_type': 'client_credentials', 'scope': DBX_M2M_SCOPE},
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DbxAuthError(f'Databricks token request to {url} failed: {exc}') from exc

    if not getattr(resp, 'ok', False):
        body = _redact(getattr(resp, 'text', ''), client_secret)
        raise DbxAuthError(
            f'Databricks service-principal token mint failed for {url}: '
            f'{resp.status_code} — {body}'
        )

    payload = resp.json() or {}
    access_token = payload.get('access_token')
    if not access_token:
        raise DbxAuthError(f'Databricks token response from {url} had no access_token')
    return TokenResponse(
        access_token=access_token,
        expires_in=int(payload.get('expires_in') or 3600),
    )


def _redact(body: str, secret: str) -> str:
    out = str(body or '')
    if secret:
        out = out.replace(secret, '[REDACTED]')
    return out[:500]
