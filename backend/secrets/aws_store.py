"""
Purpose: Production SecretStore backend on AWS Secrets Manager (FR-05 §2.1, §3), where the
SSA private keys and Databricks SP secrets actually live and RDS holds only refs. boto3 is
imported lazily inside the constructor so CI and the EC2/SQLite deployment — neither of which
has boto3 or AWS credentials — never pay for it.
"""

from __future__ import annotations

import logging

from .secret_store import SecretNotFound, SecretStoreError

logger = logging.getLogger(__name__)


class AwsSecretStore:
    """Secrets Manager backend. Secret *names* are the ref strings from FR-05 §3."""

    def __init__(self, region_name: str | None = None, client=None) -> None:
        if client is not None:
            self._client = client
            return
        try:
            import boto3  # imported here on purpose — see module docstring
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise SecretStoreError(
                'SECRET_STORE_BACKEND=aws requires boto3 — pip install -r requirements.txt'
            ) from exc
        self._client = boto3.client('secretsmanager', region_name=region_name)

    def _not_found(self) -> type[Exception]:
        return self._client.exceptions.ResourceNotFoundException

    def get_secret(self, ref: str) -> str:
        try:
            resp = self._client.get_secret_value(SecretId=ref)
        except self._not_found():
            raise SecretNotFound(ref) from None
        value = resp.get('SecretString')
        if value is None:
            binary = resp.get('SecretBinary')
            if binary is None:
                raise SecretNotFound(ref)
            value = binary.decode('utf-8')
        return value

    def put_secret(self, ref: str, value: str, *, description: str = '') -> str:
        try:
            self._client.create_secret(
                Name=ref, SecretString=value, Description=description or ref,
            )
        except self._client.exceptions.ResourceExistsException:
            self._client.put_secret_value(SecretId=ref, SecretString=value)
        return ref

    def delete_secret(self, ref: str) -> None:
        try:
            # Recovery window left at the account default so an accidental offboard
            # is reversible; FR-05 §9.3 only requires the slot to free up.
            self._client.delete_secret(SecretId=ref)
        except self._not_found():
            return

    def secret_exists(self, ref: str) -> bool:
        try:
            self._client.describe_secret(SecretId=ref)
        except self._not_found():
            return False
        return True
