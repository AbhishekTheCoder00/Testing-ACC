"""
Purpose: SSA private keys, APS client secrets and Databricks SP secrets must never sit in
the state store as plaintext (FR-05 §2.2 tiers T3/T4). This module defines the SecretStore
port every backend implements plus the AWS Secrets Manager path builders from FR-05 §3;
the database only ever persists the returned ref string, never a secret value.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

DEFAULT_PREFIX = 'forma-connector'
DEFAULT_ENV = 'dev'


class SecretStoreError(Exception):
    """Base class for secret storage failures."""


class SecretNotFound(SecretStoreError):
    """No secret is stored at the given ref."""


@runtime_checkable
class SecretStore(Protocol):
    """FR-05 §10.4. Backends: local (dev), memory (tests), aws (prod)."""

    def get_secret(self, ref: str) -> str: ...

    def put_secret(self, ref: str, value: str, *, description: str = '') -> str: ...

    def delete_secret(self, ref: str) -> None: ...

    def secret_exists(self, ref: str) -> bool: ...


# ---------------------------------------------------------------------------
# Ref naming — FR-05 §3
#
#   /{app_name}/{env}/platform/aps-app-{app_ref}-secret
#   /{app_name}/{env}/platform/data-encryption-key
#   /{app_name}/{env}/hub/{hub_id}/ssa-private-key
#   /{app_name}/{env}/connection/{connection_id}/databricks-sp-secret
#
# "RDS stores the path after leading slash as *_ref columns" — so the builders below
# return the path *without* the leading slash, and that is what goes in the database.
# ---------------------------------------------------------------------------


def _prefix() -> str:
    return (os.getenv('SECRET_STORE_PREFIX') or DEFAULT_PREFIX).strip('/')


def _env() -> str:
    return (os.getenv('APP_ENV') or DEFAULT_ENV).strip('/')


def secret_ref(*parts: str) -> str:
    """Build a store-relative secret ref. Every part must be non-empty."""
    cleaned: list[str] = []
    for part in parts:
        value = (part or '').strip().strip('/')
        if not value:
            raise ValueError(f'secret ref component must not be empty: {parts!r}')
        cleaned.append(value)
    return '/'.join([_prefix(), _env(), *cleaned])


def aps_app_secret_ref(app_ref: str) -> str:
    """Client secret of one APS Client-ID shard (FR-04 §4.1)."""
    shard = (app_ref or '').strip()
    if not shard:
        raise ValueError('app_ref must not be empty')
    return secret_ref('platform', f'aps-app-{shard}-secret')


def data_encryption_key_ref() -> str:
    """Tier T6 — the DEK used for the U2M OAuth token columns."""
    return secret_ref('platform', 'data-encryption-key')


def ssa_private_key_ref(hub_id: str) -> str:
    """Tier T3 — one robot private key per hub. Never in the database."""
    return secret_ref('hub', hub_id, 'ssa-private-key')


def dbx_sp_secret_ref(connection_id: str) -> str:
    """Tier T4 — one Databricks service-principal secret per connection."""
    return secret_ref('connection', connection_id, 'databricks-sp-secret')
