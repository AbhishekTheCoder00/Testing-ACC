"""
Purpose: v1 serves 10 enterprises on one APS Client ID, which is exactly the APS service-account
ceiling — so the capacity guard is the difference between enterprise 11 seeing a friendly
"capacity reached" panel and seeing a raw 400 cs-16 from Autodesk. These tests pin the ceiling,
the 80% ops warning, and that a hub always mints under the app it was provisioned with.
Traces FR-04 TC-01, TC-02, TC-06, TC-07, TC-09; FR-06 §4.8 step 2 quota-blocked variant.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    ssa_repository as ssa,
    tenant_repository as tenants,
)
from backend.services import ops_alert  # noqa: E402
from backend.services.m2m import aps_app_service as svc  # noqa: E402


class _CapacityTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        ops_alert.reset()
        self.addCleanup(ops_alert.reset)
        self.alerts = []
        for name in ('send_ssa_quota_warning', 'send_ssa_capacity_exhausted'):
            patcher = mock.patch.object(
                ops_alert, name,
                lambda *a, _n=name, **kw: self.alerts.append((_n, a, kw)),
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def provision(self, n: int, app_ref: str = 'app_a'):
        """Create n hubs each holding one robot under app_ref."""
        for i in range(n):
            hub = f'b.hub{i}'
            tenants.ensure_tenant(hub, app_ref)
            ssa.insert(
                hub_id=hub, aps_app_ref=app_ref, service_account_id=f'svc-{app_ref}-{i}',
                robot_email=f'r{i}@x.adskserviceaccount.autodesk.com',
                key_id='kid', private_key_ref=f'ref/{hub}',
            )


class SingleClientIdCapacityTests(_CapacityTestCase):
    """v1: one Client ID, ten enterprises, ceiling at exactly ten."""

    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a', max_robots=10)

    def test_first_hub_gets_the_only_app(self):
        """FR-04 TC-01."""
        self.assertEqual(svc.pick_app_with_capacity()['app_ref'], 'app_a')

    def test_tenth_hub_still_fits(self):
        """FR-04 TC-02 — nine used, the tenth enterprise must still onboard."""
        self.provision(9)
        self.assertEqual(svc.pick_app_with_capacity()['app_ref'], 'app_a')

    def test_eleventh_hub_is_refused(self):
        """FR-04 TC-07 — the ceiling. Enterprise 11 cannot be provisioned."""
        self.provision(10)
        self.assertIsNone(svc.pick_app_with_capacity())

    def test_precheck_reports_capacity_exhausted_with_friendly_copy(self):
        self.provision(10)
        result = svc.precheck_provision('b.hub-new')
        self.assertEqual(result.status, 'capacity_exhausted')
        self.assertIn('capacity', result.message.lower())
        self.assertNotIn('cs-16', result.message)
        self.assertIsNone(result.client_id)

    def test_precheck_ready_reports_the_client_id_and_slots(self):
        self.provision(3)
        result = svc.precheck_provision('b.hub-new')
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.client_id, 'AAA')
        self.assertEqual(result.slots_remaining, 7)

    def test_precheck_short_circuits_an_already_provisioned_hub(self):
        """FR-04 TC-06 / CS-01 — never touch the create API for a hub that has a robot."""
        self.provision(1)
        result = svc.precheck_provision('b.hub0')
        self.assertEqual(result.status, 'already_provisioned')
        self.assertEqual(result.client_id, 'AAA')

    def test_ops_warned_at_eighty_percent(self):
        self.provision(8)
        svc.pick_app_with_capacity()
        self.assertEqual([a[0] for a in self.alerts], ['send_ssa_quota_warning'])

    def test_no_warning_below_the_threshold(self):
        self.provision(7)
        svc.pick_app_with_capacity()
        self.assertEqual(self.alerts, [])

    def test_ops_alerted_when_exhausted(self):
        self.provision(10)
        svc.pick_app_with_capacity()
        self.assertEqual([a[0] for a in self.alerts], ['send_ssa_capacity_exhausted'])

    def test_offboarding_frees_a_slot(self):
        """FR-04 TC-09 — a churned enterprise releases capacity for a new one."""
        self.provision(10)
        self.assertIsNone(svc.pick_app_with_capacity())
        ssa.delete('b.hub3')
        self.assertEqual(svc.pick_app_with_capacity()['app_ref'], 'app_a')

    def test_count_is_derived_not_stored(self):
        self.provision(4)
        self.assertEqual(svc.robot_count('app_a'), 4)
        ssa.delete('b.hub1')
        self.assertEqual(svc.robot_count('app_a'), 3)

    def test_has_capacity(self):
        self.provision(10)
        self.assertFalse(svc.has_capacity('app_a'))
        self.assertFalse(svc.has_capacity('nonexistent'))


class TokenMintingAppTests(_CapacityTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a', max_robots=10)

    def test_hub_mints_under_the_app_it_was_provisioned_with(self):
        """FR-04 TC-04 — resolved from tenants.aps_app_ref, never from env or 'first app'."""
        tenants.ensure_tenant('b.hub1', 'app_a')
        self.assertEqual(svc.get_app_for_hub('b.hub1')['client_id'], 'AAA')

    def test_unknown_hub_raises(self):
        with self.assertRaises(svc.ApsAppUnavailable):
            svc.get_app_for_hub('b.nope')

    def test_unregistered_app_reference_raises(self):
        tenants.ensure_tenant('b.hub1', 'app_a')
        apps.set_active('app_a', False)
        with mock.patch.object(apps, 'get', return_value=None):
            with self.assertRaises(svc.ApsAppUnavailable):
                svc.get_app_for_hub('b.hub1')


class NoAppsConfiguredTests(_CapacityTestCase):
    def test_empty_registry_is_capacity_exhausted(self):
        """A deployment with no APS_CLIENT_ID must fail loudly, not silently pick nothing."""
        self.assertIsNone(svc.pick_app_with_capacity())
        self.assertEqual(
            svc.precheck_provision('b.hub1').status, 'capacity_exhausted',
        )

    def test_inactive_app_is_skipped(self):
        apps.insert('app_a', 'AAA', 'ref/a', max_robots=10)
        apps.set_active('app_a', False)
        self.assertIsNone(svc.pick_app_with_capacity())


class OpsAlertTests(unittest.TestCase):
    def setUp(self):
        ops_alert.reset()
        self.addCleanup(ops_alert.reset)

    def test_warning_is_emitted_once_per_count(self):
        with self.assertLogs('backend.services.ops_alert', level='ERROR') as logs:
            ops_alert.send_ssa_quota_warning('app_a', 'AAA', 8, 10)
            ops_alert.send_ssa_quota_warning('app_a', 'AAA', 8, 10)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('8/10', logs.output[0])

    def test_a_higher_count_alerts_again(self):
        with self.assertLogs('backend.services.ops_alert', level='ERROR') as logs:
            ops_alert.send_ssa_quota_warning('app_a', 'AAA', 8, 10)
            ops_alert.send_ssa_quota_warning('app_a', 'AAA', 9, 10)
        self.assertEqual(len(logs.output), 2)

    def test_exhausted_alert_names_the_remedy(self):
        with self.assertLogs('backend.services.ops_alert', level='ERROR') as logs:
            ops_alert.send_ssa_capacity_exhausted({'app_a': 10})
        self.assertIn('quota', logs.output[0].lower())

    def test_webhook_failure_never_breaks_provisioning(self):
        import os

        with mock.patch.dict(os.environ, {'OPS_ALERT_WEBHOOK_URL': 'http://127.0.0.1:1/x'}):
            with self.assertLogs('backend.services.ops_alert', level='ERROR'):
                ops_alert.send_ssa_capacity_exhausted({'app_a': 10})


if __name__ == '__main__':
    unittest.main()
