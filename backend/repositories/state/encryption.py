
import os

from cryptography.fernet import Fernet
import base64
import hashlib

# Fernet key must be 32 url-safe base64-encoded bytes.
# Derive it from SECRET_KEY by padding/hashing — or supply a separate FERNET_KEY env var.
def _get_fernet() -> Fernet:
    raw = os.getenv('SECRET_KEY', '')
    if not raw:
        raise RuntimeError('SECRET_KEY environment variable is not set')
    import base64
    import hashlib
    key_bytes = hashlib.sha256(raw.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _get_fernet().decrypt(value.encode()).decode()

