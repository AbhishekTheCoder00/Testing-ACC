"""
Purpose: In-process SecretStore backend for unit tests, so a test can exercise SSA
provisioning and token minting without touching disk or AWS. Plain dict keyed by ref;
deliberately not thread-safe beyond the GIL and deliberately not persistent.
"""

from __future__ import annotations

from .secret_store import SecretNotFound


class MemorySecretStore:
    """Volatile SecretStore. Tests only — never selected outside SECRET_STORE_BACKEND=memory."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get_secret(self, ref: str) -> str:
        try:
            return self._values[ref]
        except KeyError:
            raise SecretNotFound(ref) from None

    def put_secret(self, ref: str, value: str, *, description: str = '') -> str:
        self._values[ref] = value
        return ref

    def delete_secret(self, ref: str) -> None:
        self._values.pop(ref, None)

    def secret_exists(self, ref: str) -> bool:
        return ref in self._values
