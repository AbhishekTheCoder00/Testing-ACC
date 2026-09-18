"""
Purpose: One robot per hub, and never two — the APS quota is ten per Client ID across every
customer, so a duplicate is lost capacity, not untidiness. These tests count APS create calls
rather than just asserting success: the guarantee is "zero create calls when a robot exists",
which is invisible to a test that only checks the returned row. Also covers the two-admin race,
capacity exhaustion, the hub-admin gate, and orphan adoption after a mid-provision failure.
Traces TC-SSA-01/02/03/07/12, TC-CONC-01/02/06, FR-04 TC-06, FR-02 FR-28.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.clients.acc import ssa_client  # noqa: E402
from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    ssa_repository as ssa,
    tenant_repository as tenants,
)
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import (  # noqa: E402
    hub_admin_service,
    ssa_provisioner,
    ssa_token_service,
)

HUB = 'b.hub-aaa'
PEM = '-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n'


class FakeSsaApi:
    """Counts the calls that consume APS quota."""

    def __init__(self, *, create_raises=None, key_raises=None):
        self.create_calls: list[str] = []
        self.key_calls: list[str] = []
        self.deleted: list[tuple[str, str]] = []
        self._create_raises = create_raises
        self._key_raises = key_raises
        self._n = 0

    def get_admin_token(self, client_id, client_secret):
        return 'admin-tok'

    def create_service_account(self, admin_token, *, hub_id, name=None):
        self.create_calls.append(hub_id)
        if self._create_raises:
            raise self._create_raises
        self._n += 1
        return ssa_client.ServiceAccount(
            service_account_id=f'svc-{self._n}',
            email=f'forma-dbx-{self._n}@AAA.adskserviceaccount.autodesk.com',
        )

    def create_key(self, admin_token, service_account_id):
        self.key_calls.append(service_account_id)
        if self._key_raises:
            raise self._key_raises
        return ssa_client.ServiceAccountKey(
            kid=f'kid-{len(self.key_calls)}', private_key_pem=PEM,
        )

    def delete_key(self, admin_token, service_account_id, key_id):
        self.deleted.append((service_account_id, key_id))


class _ProvisionTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/aps-app-a-secret', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-a-secret', 'app-secret')
        ssa_token_service.invalidate()
        self.addCleanup(ssa_token_service.invalidate)

        env = mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'permissive'})
        env.start()
        self.addCleanup(env.stop)
        hub_admin_service.invalidate()
        self.addCleanup(hub_admin_service.invalidate)

    def install_api(self, **kwargs) -> FakeSsaApi:
        api = FakeSsaApi(**kwargs)
        for name in ('get_admin_token', 'create_service_account', 'create_key', 'delete_key'):
            patcher = mock.patch.object(ssa_client, name, getattr(api, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        return api


class HappyPathTests(_ProvisionTestCase):
    def test_creates_tenant_robot_and_stores_the_key_in_the_vault(self):
        api = self.install_api()
        credential = ssa_provisioner.ensure_ssa(HUB)

        self.assertEqual(api.create_calls, [HUB])
        self.assertEqual(credential['service_account_id'], 'svc-1')
        self.assertIn('adskserviceaccount', credential['robot_email'])
        self.assertEqual(get_secret_store().get_secret(credential['private_key_ref']), PEM)

    def test_row_holds_a_ref_never_the_key(self):
        """NFR-01 / TC-SSA-04."""
        self.install_api()
        credential = ssa_provisioner.ensure_ssa(HUB)
        for value in credential.values():
            self.assertNotIn('BEGIN', str(value))

    def test_tenant_lands_on_pending_whitelist(self):
        """FR-03 §7.4 — the robot exists, the customer has not invited it yet."""
        self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(tenants.get(HUB)['onboarding_status'], 'pending_whitelist')

    def test_provisioning_does_not_undo_a_verified_whitelist(self):
        """The wizard verifies the whitelist in step 1 and provisions in step 2, so a
        successful provision must leave `whitelist_verified` alone — writing
        `pending_whitelist` back sends the admin to step 1 instead of the robot email."""
        self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        tenants.update_status(HUB, 'whitelist_verified', last_error='stale failure')
        ssa.delete(HUB)  # force a real re-provision, not the CS-01 short circuit

        ssa_provisioner.ensure_ssa(HUB)

        row = tenants.get(HUB)
        self.assertEqual(row['onboarding_status'], 'whitelist_verified')
        self.assertIsNone(row['last_error'])

    def test_adoption_marker_is_cleared_on_success(self):
        self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        self.assertIsNone(tenants.get(HUB)['pending_service_account_id'])

    def test_shard_is_recorded_on_the_tenant(self):
        self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(tenants.get(HUB)['aps_app_ref'], 'app_a')


class IdempotencyTests(_ProvisionTestCase):
    def test_second_call_makes_zero_create_calls(self):
        """TC-SSA-02/03 — the guarantee that protects the quota."""
        api = self.install_api()
        first = ssa_provisioner.ensure_ssa(HUB)
        second = ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(len(api.create_calls), 1)
        self.assertEqual(first['service_account_id'], second['service_account_id'])

    def test_ten_repeat_calls_still_one_robot(self):
        """TC-SSA-12 — a retry storm must not exceed the limit."""
        api = self.install_api()
        for _ in range(10):
            ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(len(api.create_calls), 1)
        self.assertEqual(ssa.count_by_app_ref('app_a'), 1)

    def test_second_admin_reuses_the_robot(self):
        """TC-SSA-08 / Scenario B — Bob after Alice."""
        api = self.install_api()
        ssa_provisioner.ensure_ssa(HUB, aps_user_id='alice')
        ssa_provisioner.ensure_ssa(HUB, aps_user_id='bob')
        self.assertEqual(len(api.create_calls), 1)

    def test_different_hubs_get_different_robots(self):
        api = self.install_api()
        a = ssa_provisioner.ensure_ssa('b.hub-a')
        b = ssa_provisioner.ensure_ssa('b.hub-b')
        self.assertEqual(len(api.create_calls), 2)
        self.assertNotEqual(a['service_account_id'], b['service_account_id'])

    def test_concurrent_provision_creates_exactly_one_robot_at_aps(self):
        """TC-CONC-01/02/06 and FR-02 FR-28 — four admins click Provision at once.

        Asserts the APS *create call count*, not the row count. Counting rows passes even when
        several robots were created and all but one orphaned — which is the actual quota leak
        this guards against, since each orphan permanently holds one of ten slots.
        """
        api = self.install_api()
        barrier = threading.Barrier(4)
        errors: list[Exception] = []
        results: list[dict] = []

        def worker():
            barrier.wait()
            try:
                results.append(ssa_provisioner.ensure_ssa(HUB))
            except Exception as exc:  # collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(api.create_calls), 1, 'more than one APS robot was created')
        self.assertEqual(len(api.key_calls), 1)
        self.assertEqual(ssa.count_by_app_ref('app_a'), 1)
        self.assertEqual(len({r['service_account_id'] for r in results}), 1)

    def test_a_stale_claim_does_not_lock_the_hub_out(self):
        """A worker that dies mid-provision must not block the hub permanently."""
        self.install_api()
        tenants.ensure_tenant(HUB, 'app_a')
        self.assertTrue(tenants.try_claim_provisioning(HUB))
        self.assertFalse(tenants.try_claim_provisioning(HUB))
        self.assertTrue(tenants.try_claim_provisioning(HUB, stale_after=0))

    def test_claim_is_released_after_a_failure(self):
        self.install_api(key_raises=RuntimeError('vault down'))
        with self.assertRaises(ssa_provisioner.SsaProvisionError):
            ssa_provisioner.ensure_ssa(HUB)
        self.assertIsNone(tenants.get(HUB)['provisioning_claimed_at'])


class CapacityTests(_ProvisionTestCase):
    def _fill(self, n):
        for i in range(n):
            hub = f'b.filler{i}'
            tenants.ensure_tenant(hub, 'app_a')
            ssa.insert(hub_id=hub, aps_app_ref='app_a', service_account_id=f'filler-{i}',
                       robot_email=f'f{i}@x', key_id='k', private_key_ref=f'ref/{hub}')

    def test_eleventh_hub_raises_capacity_exhausted(self):
        """FR-04 TC-07 — enterprise 11."""
        self.install_api()
        self._fill(10)
        with self.assertRaises(ssa_provisioner.SsaCapacityExhausted) as ctx:
            ssa_provisioner.ensure_ssa('b.hub-new')
        self.assertIn('capacity', str(ctx.exception).lower())

    def test_no_partial_tenant_row_when_capacity_is_gone(self):
        """A tenant needs an aps_app_ref, and there is no app with room to assign."""
        self.install_api()
        self._fill(10)
        with self.assertRaises(ssa_provisioner.SsaCapacityExhausted):
            ssa_provisioner.ensure_ssa('b.hub-new')
        self.assertIsNone(tenants.get('b.hub-new'))

    def test_no_create_call_is_attempted_when_full(self):
        """The pre-check exists so the customer never sees a raw APS quota error."""
        api = self.install_api()
        self._fill(10)
        with self.assertRaises(ssa_provisioner.SsaCapacityExhausted):
            ssa_provisioner.ensure_ssa('b.hub-new')
        self.assertEqual(api.create_calls, [])

    def test_aps_quota_error_is_translated_for_an_existing_tenant(self):
        """If APS rejects despite our pre-check, the hub is marked, not left silent."""
        self.install_api(create_raises=ssa_client.SsaQuotaExceeded('cs-16 limit'))
        tenants.ensure_tenant(HUB, 'app_a')
        with self.assertRaises(ssa_provisioner.SsaCapacityExhausted):
            ssa_provisioner.ensure_ssa(HUB)
        row = tenants.get(HUB)
        self.assertEqual(row['onboarding_status'], 'ssa_limit_reached')
        self.assertIn('cs-16', row['last_error'])


class OrphanAdoptionTests(_ProvisionTestCase):
    def test_key_failure_leaves_no_credential_but_records_the_robot(self):
        api = self.install_api(key_raises=RuntimeError('vault down'))
        with self.assertRaises(ssa_provisioner.SsaProvisionError):
            ssa_provisioner.ensure_ssa(HUB)

        self.assertIsNone(ssa.get_by_hub_id(HUB))
        row = tenants.get(HUB)
        self.assertEqual(row['pending_service_account_id'], 'svc-1')
        self.assertEqual(row['onboarding_status'], 'ssa_provision_failed')
        self.assertEqual(len(api.create_calls), 1)

    def test_retry_adopts_the_orphan_instead_of_creating_a_second_robot(self):
        """The one failure the DB-exists check cannot catch on its own."""
        failing = self.install_api(key_raises=RuntimeError('vault down'))
        with self.assertRaises(ssa_provisioner.SsaProvisionError):
            ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(len(failing.create_calls), 1)

        healthy = self.install_api()          # replaces the patches
        credential = ssa_provisioner.ensure_ssa(HUB)

        self.assertEqual(healthy.create_calls, [])            # no second robot
        self.assertEqual(healthy.key_calls, ['svc-1'])        # key made on the orphan
        self.assertEqual(credential['service_account_id'], 'svc-1')
        self.assertEqual(ssa.count_by_app_ref('app_a'), 1)

    def test_vault_write_failure_is_also_adoptable(self):
        self.install_api()
        with mock.patch.object(
            ssa_provisioner, 'get_secret_store',
            side_effect=RuntimeError('KMS denied'),
        ):
            with self.assertRaises(ssa_provisioner.SsaProvisionError):
                ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(tenants.get(HUB)['pending_service_account_id'], 'svc-1')


class AdminGateTests(_ProvisionTestCase):
    def test_non_admin_cannot_consume_a_slot(self):
        api = self.install_api()
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}), \
             mock.patch.object(hub_admin_service, 'check_hub_admin',
                               return_value=hub_admin_service.NOT_ADMIN):
            with self.assertRaises(hub_admin_service.AccessDenied):
                ssa_provisioner.ensure_ssa(HUB, aps_user_id='casey')
        self.assertEqual(api.create_calls, [])
        self.assertIsNone(ssa.get_by_hub_id(HUB))

    def test_unverified_hub_cannot_provision(self):
        api = self.install_api()
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}), \
             mock.patch.object(hub_admin_service, 'check_hub_admin',
                               return_value=hub_admin_service.UNVERIFIED):
            with self.assertRaises(hub_admin_service.AccessDenied) as ctx:
                ssa_provisioner.ensure_ssa(HUB, aps_user_id='alice')
        self.assertEqual(ctx.exception.reason, 'unverified')
        self.assertEqual(api.create_calls, [])

    def test_confirmed_admin_provisions(self):
        api = self.install_api()
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}), \
             mock.patch.object(hub_admin_service, 'check_hub_admin',
                               return_value=hub_admin_service.ADMIN):
            ssa_provisioner.ensure_ssa(HUB, aps_user_id='alice')
        self.assertEqual(len(api.create_calls), 1)

    def test_no_user_id_skips_the_gate_for_internal_callers(self):
        """The scheduler has no session; the CS-01 short circuit covers the normal case."""
        api = self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        self.assertEqual(len(api.create_calls), 1)


class RotateKeyTests(_ProvisionTestCase):
    def test_rotation_keeps_the_robot_and_replaces_the_key(self):
        """FR-03 §12.3 — rotation must never create a second robot."""
        api = self.install_api()
        original = ssa_provisioner.ensure_ssa(HUB)

        with mock.patch.object(ssa_token_service, 'get_acc_token', return_value='tok'):
            rotated = ssa_provisioner.rotate_key(HUB)

        self.assertEqual(api.create_calls, [HUB])                  # still one create, total
        self.assertEqual(rotated['service_account_id'], original['service_account_id'])
        self.assertNotEqual(rotated['key_id'], original['key_id'])
        self.assertIsNotNone(rotated['rotated_at'])

    def test_old_key_is_deleted_last(self):
        api = self.install_api()
        original = ssa_provisioner.ensure_ssa(HUB)
        with mock.patch.object(ssa_token_service, 'get_acc_token', return_value='tok'):
            ssa_provisioner.rotate_key(HUB)
        self.assertEqual(api.deleted, [(original['service_account_id'], original['key_id'])])

    def test_cached_token_is_invalidated_before_verification(self):
        self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        calls = []
        with mock.patch.object(ssa_token_service, 'invalidate',
                               side_effect=lambda h=None: calls.append(('invalidate', h))), \
             mock.patch.object(ssa_token_service, 'get_acc_token',
                               side_effect=lambda h, force=False: calls.append(('mint', force)) or 'tok'):
            ssa_provisioner.rotate_key(HUB)
        self.assertEqual(calls, [('invalidate', HUB), ('mint', True)])

    def test_failure_to_delete_the_old_key_does_not_fail_rotation(self):
        api = self.install_api()
        ssa_provisioner.ensure_ssa(HUB)
        with mock.patch.object(ssa_client, 'delete_key',
                               side_effect=RuntimeError('APS hiccup')), \
             mock.patch.object(ssa_token_service, 'get_acc_token', return_value='tok'):
            rotated = ssa_provisioner.rotate_key(HUB)
        self.assertIsNotNone(rotated['rotated_at'])

    def test_rotating_a_hub_with_no_robot_raises(self):
        self.install_api()
        with self.assertRaises(ssa_provisioner.SsaProvisionError):
            ssa_provisioner.rotate_key('b.nope')


class NoAppRegisteredTests(TempDbTestCase):
    def test_missing_registry_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'permissive'}):
            with self.assertRaises(ssa_provisioner.SsaCapacityExhausted):
                ssa_provisioner.ensure_ssa(HUB)


if __name__ == '__main__':
    unittest.main()
