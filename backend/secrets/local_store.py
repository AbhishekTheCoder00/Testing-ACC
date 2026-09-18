"""
Purpose: Local-disk SecretStore backend for development, so the connector runs with no AWS
account while still keeping SSA private keys and SP secrets out of the state store. One
Fernet-encrypted file per ref, reusing the existing SECRET_KEY-derived key from
state/encryption.py; the ref's slashes are flattened to '__' to give a safe flat filename.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.repositories.state.encryption import decrypt, encrypt

from .secret_store import SecretNotFound, SecretStoreError


def _filename(ref: str) -> str:
    """Flatten a ref to a filename. '/' is the only separator refs use (see FR-05 §3)."""
    return ref.strip('/').replace('/', '__')


class LocalSecretStore:
    """Fernet-encrypted files under a directory. Not for production — see aws_store."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._dir = Path(directory)

    def _path(self, ref: str) -> Path:
        name = _filename(ref)
        if not name:
            raise ValueError('secret ref must not be empty')
        return self._dir / name

    def get_secret(self, ref: str) -> str:
        path = self._path(ref)
        if not path.is_file():
            raise SecretNotFound(ref)
        try:
            return decrypt(path.read_text(encoding='utf-8'))
        except SecretNotFound:
            raise
        except Exception as exc:  # corrupt file or rotated SECRET_KEY
            raise SecretStoreError(
                f'could not decrypt secret {ref} — was SECRET_KEY rotated?'
            ) from exc

    def put_secret(self, ref: str, value: str, *, description: str = '') -> str:
        path = self._path(ref)
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(encrypt(value), encoding='utf-8')
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows and some mounts do not honour POSIX modes; the Fernet layer is
            # the real protection here, the mode is defence in depth.
            pass
        return ref

    def delete_secret(self, ref: str) -> None:
        self._path(ref).unlink(missing_ok=True)

    def secret_exists(self, ref: str) -> bool:
        return self._path(ref).is_file()
