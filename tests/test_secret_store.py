"""
Purpose: Lock in the SecretStore contract that keeps SSA private keys, APS client secrets
and Databricks SP secrets out of the state store (FR-05 §2.2 tiers T3/T4, §3 naming, §10.4).
Covers the memory and local backends end to end, the exact Secrets Manager path shapes, and
the rule that selecting a non-aws backend never pulls in boto3. Traces TC-SSA-04, TC-NFR-01.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')

from backend.secrets import (  # noqa: E402
    SecretNotFound,
    aps_app_secret_ref,
    data_encryption_key_ref,
    dbx_sp_secret_ref,
    get_secret_store,
    reset_secret_store,
    secret_ref,
    ssa_private_key_ref,
)
from backend.secrets.memory_store import MemorySecretStore  # noqa: E402


PEM = (
    '-----BEGIN PRIVATE KEY-----\n'
    'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n'
    '-----END PRIVATE KEY-----\n'
)


class SecretRefNamingTests(unittest.TestCase):
    """FR-05 §3 — refs are the Secrets Manager path with the leading slash stripped."""

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {'SECRET_STORE_PREFIX': 'forma-connector', 'APP_ENV': 'prod'},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_aps_app_secret_ref(self):
        self.assertEqual(
            aps_app_secret_ref('app_b'),
            'forma-connector/prod/platform/aps-app-app_b-secret',
        )

    def test_ssa_private_key_ref(self):
        self.assertEqual(
            ssa_private_key_ref('b.7f3a9b2c'),
            'forma-connector/prod/hub/b.7f3a9b2c/ssa-private-key',
        )

    def test_dbx_sp_secret_ref(self):
        self.assertEqual(
            dbx_sp_secret_ref('cnx_abc123'),
            'forma-connector/prod/connection/cnx_abc123/databricks-sp-secret',
        )

    def test_data_encryption_key_ref(self):
        self.assertEqual(
            data_encryption_key_ref(),
            'forma-connector/prod/platform/data-encryption-key',
        )

    def test_no_leading_slash_and_no_double_slash(self):
        for ref in (
            aps_app_secret_ref('app_a'),
            ssa_private_key_ref('b.x'),
            dbx_sp_secret_ref('cnx_y'),
            data_encryption_key_ref(),
        ):
            self.assertFalse(ref.startswith('/'), ref)
            self.assertNotIn('//', ref)

    def test_env_and_prefix_are_configurable(self):
        with mock.patch.dict(
            os.environ,
            {'SECRET_STORE_PREFIX': 'other-app', 'APP_ENV': 'dev'},
        ):
            self.assertEqual(
                ssa_private_key_ref('b.x'), 'other-app/dev/hub/b.x/ssa-private-key',
            )

    def test_defaults_match_fr05(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SECRET_STORE_PREFIX', None)
            os.environ.pop('APP_ENV', None)
            self.assertEqual(secret_ref('platform', 'x'), 'forma-connector/dev/platform/x')

    def test_empty_component_is_rejected(self):
        with self.assertRaises(ValueError):
            ssa_private_key_ref('')
        with self.assertRaises(ValueError):
            dbx_sp_secret_ref('  ')


class _StoreContractMixin:
    """Behaviour every backend must satisfy."""

    def make_store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def test_put_then_get_round_trip(self):
        store = self.make_store()
        store.put_secret('a/b/c', PEM)
        self.assertEqual(store.get_secret('a/b/c'), PEM)

    def test_put_returns_the_ref(self):
        store = self.make_store()
        self.assertEqual(store.put_secret('a/b/c', 'v'), 'a/b/c')

    def test_secret_exists(self):
        store = self.make_store()
        self.assertFalse(store.secret_exists('a/b/c'))
        store.put_secret('a/b/c', 'v')
        self.assertTrue(store.secret_exists('a/b/c'))

    def test_get_missing_raises(self):
        store = self.make_store()
        with self.assertRaises(SecretNotFound):
            store.get_secret('nope/missing')

    def test_put_overwrites(self):
        store = self.make_store()
        store.put_secret('a/b/c', 'first')
        store.put_secret('a/b/c', 'second')
        self.assertEqual(store.get_secret('a/b/c'), 'second')

    def test_delete_then_gone(self):
        store = self.make_store()
        store.put_secret('a/b/c', 'v')
        store.delete_secret('a/b/c')
        self.assertFalse(store.secret_exists('a/b/c'))
        with self.assertRaises(SecretNotFound):
            store.get_secret('a/b/c')

    def test_delete_missing_is_idempotent(self):
        # FR-05 §9.3 offboarding deletes without checking first.
        self.make_store().delete_secret('never/existed')

    def test_multiline_pem_survives(self):
        store = self.make_store()
        store.put_secret('hub/b.x/ssa-private-key', PEM)
        self.assertEqual(store.get_secret('hub/b.x/ssa-private-key'), PEM)

    def test_refs_are_isolated(self):
        store = self.make_store()
        store.put_secret('hub/b.one/ssa-private-key', 'one')
        store.put_secret('hub/b.two/ssa-private-key', 'two')
        self.assertEqual(store.get_secret('hub/b.one/ssa-private-key'), 'one')
        self.assertEqual(store.get_secret('hub/b.two/ssa-private-key'), 'two')


class MemorySecretStoreTests(_StoreContractMixin, unittest.TestCase):
    def make_store(self):
        return MemorySecretStore()


class LocalSecretStoreTests(_StoreContractMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='secretstore-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_store(self):
        from backend.secrets.local_store import LocalSecretStore

        return LocalSecretStore(self.tmp)

    def test_value_is_encrypted_on_disk(self):
        """The whole point of T3/T4: the plaintext must not be readable from the file."""
        store = self.make_store()
        store.put_secret('hub/b.x/ssa-private-key', PEM)
        blobs = [p.read_bytes() for p in Path(self.tmp).iterdir() if p.is_file()]
        self.assertTrue(blobs, 'local backend wrote no file')
        for blob in blobs:
            self.assertNotIn(b'BEGIN PRIVATE KEY', blob)
            self.assertNotIn(PEM.encode(), blob)

    def test_ref_maps_to_a_flat_filename(self):
        store = self.make_store()
        store.put_secret('forma-connector/dev/hub/b.x/ssa-private-key', 'v')
        names = [p.name for p in Path(self.tmp).iterdir() if p.is_file()]
        self.assertEqual(len(names), 1)
        self.assertNotIn('/', names[0])
        self.assertNotIn(os.sep, names[0])

    def test_directory_is_created_on_demand(self):
        from backend.secrets.local_store import LocalSecretStore

        nested = os.path.join(self.tmp, 'deep', 'nested')
        LocalSecretStore(nested).put_secret('a/b', 'v')
        self.assertTrue(os.path.isdir(nested))

    def test_survives_a_new_store_instance(self):
        self.make_store().put_secret('a/b', 'persisted')
        self.assertEqual(self.make_store().get_secret('a/b'), 'persisted')


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        reset_secret_store()
        self.addCleanup(reset_secret_store)

    def test_default_backend_is_local(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SECRET_STORE_BACKEND', None)
            tmp = tempfile.mkdtemp(prefix='secretstore-')
            self.addCleanup(shutil.rmtree, tmp, True)
            os.environ['SECRET_STORE_DIR'] = tmp
            from backend.secrets.local_store import LocalSecretStore

            self.assertIsInstance(get_secret_store(), LocalSecretStore)

    def test_memory_backend_selected_by_env(self):
        with mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'memory'}):
            self.assertIsInstance(get_secret_store(), MemorySecretStore)

    def test_store_is_a_singleton_until_reset(self):
        with mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'memory'}):
            first = get_secret_store()
            self.assertIs(get_secret_store(), first)
            reset_secret_store()
            self.assertIsNot(get_secret_store(), first)

    def test_unknown_backend_is_rejected_loudly(self):
        with mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'nonsense'}):
            with self.assertRaises(ValueError):
                get_secret_store()

    def test_non_aws_backend_does_not_import_boto3(self):
        """CI and the EC2 deployment have no boto3; selecting local must not need it."""
        sys.modules.pop('backend.secrets.aws_store', None)
        with mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'memory'}):
            get_secret_store()
        self.assertNotIn('backend.secrets.aws_store', sys.modules)


if __name__ == '__main__':
    unittest.main()
