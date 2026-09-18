"""
Purpose: The tenant side of the multi-tenant model — APS shard registry, hub tenants, hub-admin
mappings and SSA credential rows. The critical behaviours pinned here are that ensure_tenant
never rewrites an existing hub's aps_app_ref (FR-04 §4.2, TC-04) and that robot counts are
derived from ssa_credentials rather than a stored counter. Traces TC-USER-01/03/04/08, TC-SSA-09.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    ssa_repository as ssa,
    tenant_repository as tenants,
)


class ApsAppRepositoryTests(TempDbTestCase):
    def test_insert_and_get(self):
        apps.insert('app_a', 'AAA', 'ref/a', display_name='Primary APS App')
        row = apps.get('app_a')
        self.assertEqual(row['client_id'], 'AAA')
        self.assertEqual(row['max_robots'], 10)
        self.assertTrue(row['is_active'])

    def test_get_missing_returns_none(self):
        self.assertIsNone(apps.get('nope'))

    def test_list_active_is_ordered_deterministically(self):
        """FR-04 §5.2 — app_a must be picked before app_b, every time."""
        apps.insert('app_b', 'BBB', 'ref/b')
        apps.insert('app_a', 'AAA', 'ref/a')
        apps.insert('app_c', 'CCC', 'ref/c')
        self.assertEqual([a['app_ref'] for a in apps.list_active()], ['app_a', 'app_b', 'app_c'])

    def test_list_active_skips_inactive(self):
        apps.insert('app_a', 'AAA', 'ref/a')
        apps.insert('app_b', 'BBB', 'ref/b')
        apps.set_active('app_a', False)
        self.assertEqual([a['app_ref'] for a in apps.list_active()], ['app_b'])

    def test_get_by_client_id(self):
        apps.insert('app_a', 'AAA', 'ref/a')
        self.assertEqual(apps.get_by_client_id('AAA')['app_ref'], 'app_a')
        self.assertIsNone(apps.get_by_client_id('ZZZ'))

    def test_max_robots_is_configurable(self):
        """Fixtures set this to 1 to reach the quota-blocked states without 10 robots."""
        apps.insert('app_a', 'AAA', 'ref/a', max_robots=1)
        self.assertEqual(apps.get('app_a')['max_robots'], 1)


class TenantRepositoryTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        apps.insert('app_b', 'BBB', 'ref/b')

    def test_ensure_tenant_creates(self):
        row = tenants.ensure_tenant('b.hub1', 'app_a', acc_account_id='acct-1', hub_name='Acme')
        self.assertEqual(row['hub_id'], 'b.hub1')
        self.assertEqual(row['tenant_id'], 'b.hub1')
        self.assertEqual(row['aps_app_ref'], 'app_a')
        self.assertEqual(row['acc_account_id'], 'acct-1')
        self.assertEqual(row['onboarding_status'], 'pending_whitelist')

    def test_ensure_tenant_is_idempotent(self):
        first = tenants.ensure_tenant('b.hub1', 'app_a')
        second = tenants.ensure_tenant('b.hub1', 'app_a')
        self.assertEqual(first['created_at'], second['created_at'])

    def test_ensure_tenant_never_reassigns_the_shard(self):
        """FR-04 §4.2 / TC-04 — hub 1 keeps app_a forever, even if asked otherwise."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        again = tenants.ensure_tenant('b.hub1', 'app_b')
        self.assertEqual(again['aps_app_ref'], 'app_a')
        self.assertEqual(tenants.get('b.hub1')['aps_app_ref'], 'app_a')

    def test_ensure_tenant_does_not_reset_status(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.update_status('b.hub1', 'ssa_active')
        tenants.ensure_tenant('b.hub1', 'app_a')
        self.assertEqual(tenants.get('b.hub1')['onboarding_status'], 'ssa_active')

    def test_update_status_and_error(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.update_status('b.hub1', 'ssa_limit_reached', last_error='cs-16 limit')
        row = tenants.get('b.hub1')
        self.assertEqual(row['onboarding_status'], 'ssa_limit_reached')
        self.assertEqual(row['last_error'], 'cs-16 limit')

    def test_update_status_clears_previous_error(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.update_status('b.hub1', 'ssa_provision_failed', last_error='boom')
        tenants.update_status('b.hub1', 'whitelist_verified')
        self.assertIsNone(tenants.get('b.hub1')['last_error'])

    def test_set_ssa_verified(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.set_ssa_verified('b.hub1')
        self.assertIsNotNone(tenants.get('b.hub1')['ssa_verified_at'])

    def test_add_user_and_membership(self):
        """TC-USER-01."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        self.assertFalse(tenants.is_member('b.hub1', 'alice'))
        tenants.add_user('b.hub1', 'alice')
        self.assertTrue(tenants.is_member('b.hub1', 'alice'))
        self.assertEqual(tenants.list_users('b.hub1')[0]['role'], 'hub_admin')

    def test_add_user_is_idempotent(self):
        """TC-USER-03 — a repeat login must not duplicate the mapping."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.add_user('b.hub1', 'alice')
        tenants.add_user('b.hub1', 'alice')
        self.assertEqual(len(tenants.list_users('b.hub1')), 1)

    def test_second_admin_joins_without_touching_the_tenant(self):
        """TC-USER-02 / Scenario B."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.add_user('b.hub1', 'alice')
        tenants.add_user('b.hub1', 'bob')
        self.assertEqual(
            sorted(u['aps_user_id'] for u in tenants.list_users('b.hub1')), ['alice', 'bob'],
        )

    def test_role_is_always_hub_admin(self):
        """TC-AUTH-08 / FR-07 — project_admin and viewer are not supported."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        with self.assertRaises(ValueError):
            tenants.add_user('b.hub1', 'casey', role='viewer')
        self.assertEqual(tenants.list_users('b.hub1'), [])

    def test_same_user_maps_to_multiple_hubs_independently(self):
        """TC-USER-04 / Scenario C."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.ensure_tenant('b.hub2', 'app_a')
        tenants.add_user('b.hub1', 'alice')
        tenants.add_user('b.hub2', 'alice')
        hubs = [h['hub_id'] for h in tenants.list_hubs_for_user('alice')]
        self.assertEqual(sorted(hubs), ['b.hub1', 'b.hub2'])

    def test_list_hubs_for_user_excludes_other_peoples_hubs(self):
        """TC-AUTH-05 / TC-USER-09 — no cross-hub leakage."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.ensure_tenant('b.hub2', 'app_a')
        tenants.add_user('b.hub1', 'alice')
        tenants.add_user('b.hub2', 'bob')
        self.assertEqual([h['hub_id'] for h in tenants.list_hubs_for_user('alice')], ['b.hub1'])

    def test_is_member_false_for_unknown_hub(self):
        self.assertFalse(tenants.is_member('b.nope', 'alice'))

    def test_delete_tenant_removes_users(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.add_user('b.hub1', 'alice')
        tenants.delete_tenant('b.hub1')
        self.assertIsNone(tenants.get('b.hub1'))
        self.assertEqual(tenants.list_users('b.hub1'), [])


class SsaRepositoryTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        apps.insert('app_b', 'BBB', 'ref/b')
        tenants.ensure_tenant('b.hub1', 'app_a')
        tenants.ensure_tenant('b.hub2', 'app_a')

    def _insert(self, hub_id='b.hub1', app_ref='app_a', sa_id='svc-1'):
        return ssa.insert(
            hub_id=hub_id,
            aps_app_ref=app_ref,
            service_account_id=sa_id,
            robot_email=f'{sa_id}@AAA.adskserviceaccount.autodesk.com',
            key_id='kid-1',
            private_key_ref=f'forma-connector/dev/hub/{hub_id}/ssa-private-key',
        )

    def test_insert_and_get(self):
        self._insert()
        row = ssa.get_by_hub_id('b.hub1')
        self.assertEqual(row['service_account_id'], 'svc-1')
        self.assertEqual(row['tenant_id'], 'b.hub1')
        self.assertIn('ssa-private-key', row['private_key_ref'])

    def test_row_holds_a_ref_not_a_key(self):
        """NFR-01 / TC-SSA-04 — no PEM anywhere in the row."""
        self._insert()
        row = ssa.get_by_hub_id('b.hub1')
        for value in row.values():
            self.assertNotIn('BEGIN', str(value))

    def test_exists(self):
        self.assertFalse(ssa.exists('b.hub1'))
        self._insert()
        self.assertTrue(ssa.exists('b.hub1'))

    def test_count_by_app_ref_is_derived(self):
        """FR-05 §9.1 — the quota count comes from the rows, not a counter column."""
        self.assertEqual(ssa.count_by_app_ref('app_a'), 0)
        self._insert('b.hub1', 'app_a', 'svc-1')
        self._insert('b.hub2', 'app_a', 'svc-2')
        self.assertEqual(ssa.count_by_app_ref('app_a'), 2)
        self.assertEqual(ssa.count_by_app_ref('app_b'), 0)

    def test_update_key_rotates_without_touching_the_robot(self):
        """FR-03 §12.3."""
        self._insert()
        ssa.update_key('b.hub1', key_id='kid-2', private_key_ref='ref/new')
        row = ssa.get_by_hub_id('b.hub1')
        self.assertEqual(row['key_id'], 'kid-2')
        self.assertEqual(row['private_key_ref'], 'ref/new')
        self.assertEqual(row['service_account_id'], 'svc-1')
        self.assertIsNotNone(row['rotated_at'])

    def test_delete_frees_the_slot(self):
        """FR-04 TC-09 — offboarding a hub lets a new hub use that app."""
        self._insert('b.hub1', 'app_a', 'svc-1')
        self.assertEqual(ssa.count_by_app_ref('app_a'), 1)
        ssa.delete('b.hub1')
        self.assertEqual(ssa.count_by_app_ref('app_a'), 0)
        self.assertIsNone(ssa.get_by_hub_id('b.hub1'))

    def test_deleting_the_tenant_cascades_in_the_repository(self):
        self._insert('b.hub1', 'app_a', 'svc-1')
        tenants.delete_tenant('b.hub1')
        self.assertIsNone(ssa.get_by_hub_id('b.hub1'))
        self.assertEqual(ssa.count_by_app_ref('app_a'), 0)


if __name__ == '__main__':
    unittest.main()
