"""
Purpose: Entry point for secret storage. Callers use get_secret_store() and the ref builders
rather than constructing a backend, so the choice of local/memory/aws is one env var and the
FR-05 §3 path shapes are defined in exactly one place. Backend modules are imported lazily so
a deployment without boto3 can still run.
"""

from __future__ import annotations

import os
import threading

from .secret_store import (
    DEFAULT_ENV,
    DEFAULT_PREFIX,
    SecretNotFound,
    SecretStore,
    SecretStoreError,
    aps_app_secret_ref,
    data_encryption_key_ref,
    dbx_sp_secret_ref,
    secret_ref,
    ssa_private_key_ref,
)

__all__ = [
    'DEFAULT_ENV',
    'DEFAULT_PREFIX',
    'SecretNotFound',
    'SecretStore',
    'SecretStoreError',
    'aps_app_secret_ref',
    'data_encryption_key_ref',
    'dbx_sp_secret_ref',
    'get_secret_store',
    'reset_secret_store',
    'secret_ref',
    'ssa_private_key_ref',
]

_DEFAULT_LOCAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'secrets',
    'store',
)

_store: SecretStore | None = None
_lock = threading.Lock()


def _build_store() -> SecretStore:
    backend = (os.getenv('SECRET_STORE_BACKEND') or 'local').strip().lower()
    if backend == 'local':
        from .local_store import LocalSecretStore

        return LocalSecretStore(os.getenv('SECRET_STORE_DIR') or _DEFAULT_LOCAL_DIR)
    if backend == 'memory':
        from .memory_store import MemorySecretStore

        return MemorySecretStore()
    if backend == 'aws':
        from .aws_store import AwsSecretStore

        return AwsSecretStore(region_name=os.getenv('AWS_REGION') or None)
    raise ValueError(
        f'unknown SECRET_STORE_BACKEND {backend!r} — expected local, memory or aws'
    )


def get_secret_store() -> SecretStore:
    """Process-wide SecretStore, built on first use from SECRET_STORE_BACKEND."""
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = _build_store()
    return _store


def reset_secret_store() -> None:
    """Drop the cached backend. For tests and for switching backends at runtime."""
    global _store
    with _lock:
        _store = None
