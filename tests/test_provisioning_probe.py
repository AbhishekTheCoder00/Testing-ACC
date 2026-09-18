"""
Purpose: The manual ACC steps have no "was this done?" API, so these probes infer the answer
from how real calls respond — which makes their failure mapping the whole product value. These
tests pin that every failure names the exact remediation, that probe 1 needs no robot and mints
no SSA token, and above all that probes 2/3 re-run for a second project on an already-verified
hub — the FR-03 §8.2 per-hub caching bug this redesign exists to fix. Traces TC-WL-01…06.
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
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import provisioning_probe as probe  # noqa: E402

HUB_A = 'b.hub-aaa'
HUB_B = 'b.hub-bbb'
HUB_C = 'b.hub-ccc'  # deliberately has no tenant row — the first-time-hub path
ROBOT = 'forma-dbx-aaa@autodesk-svc.com'


class _HttpError(Exception):
    """Stands in for the client's raised HTTP failure, which carries a response."""

    def __init__(self, status: int):
        super().__init__(f'HTTP {status}')
        self.response = mock.Mock(status_code=status)


class _ProbeTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'CLIENT-AAA', 'ref/aps-app-a', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-a', 'app-secret')
        tenants.ensure_tenant(HUB_A, 'app_a')
        tenants.ensure_tenant(HUB_B, 'app_a')

    def give_robot(self, hub_id=HUB_A, email=ROBOT):
        ssa.insert(
            hub_id=hub_id,
            aps_app_ref='app_a',
            service_account_id=f'sa-{hub_id}',
            robot_email=email,
            key_id='key-1',
            private_key_ref=f'ref/{hub_id}/key',
        )


class AppAuthorisedTests(_ProbeTestCase):
    def test_a_pass_records_the_verification_and_advances_the_status(self):
        with mock.patch.object(probe, '_get', return_value={'data': []}):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                result = probe.verify_app_authorised(HUB_A)
        self.assertTrue(result.passed)
        tenant = tenants.get(HUB_A)
        self.assertEqual(tenant['onboarding_status'], 'whitelist_verified')
        self.assertIsNotNone(tenant['ssa_verified_at'])

    def test_probe_one_asks_for_the_scope_its_endpoint_requires(self):
        """Regression: probe 1 calls Data Management (`/project/v1/hubs/.../projects`), which
        needs `data:read`. It used to reuse the Account-Admin token, which carries only
        `account:read` — Autodesk then returns 403 no matter how correctly the customer
        whitelisted the Client ID, so the whitelist step could never be passed."""
        with mock.patch.object(probe, '_get', return_value={'data': []}):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token') as mint:
                probe.verify_app_authorised(HUB_A)
        scope = mint.call_args.kwargs.get('scope', '')
        self.assertIn('data:read', scope)

    def test_probe_one_needs_no_robot_and_mints_no_ssa_token(self):
        """D-15 — otherwise wizard step 1 would deadlock against step 2."""
        from backend.services.m2m import ssa_token_service

        with mock.patch.object(ssa_token_service, 'get_acc_token',
                               side_effect=AssertionError('minted an SSA token')):
            with mock.patch.object(probe, '_get', return_value={'data': []}):
                with mock.patch('backend.clients.acc.admin_client.get_account_token',
                                return_value='2lo-token'):
                    result = probe.verify_app_authorised(HUB_A)
        self.assertTrue(result.passed)
        self.assertIsNone(ssa.get_by_hub_id(HUB_A))

    def test_a_403_names_the_client_id_and_the_acc_path(self):
        """TC-WL-02 — the failure has to be actionable without support."""
        with mock.patch.object(probe, '_get', side_effect=_HttpError(403)):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                result = probe.verify_app_authorised(HUB_A)
        self.assertFalse(result.passed)
        self.assertEqual(result.code, 'client_id_not_whitelisted')
        self.assertIn('CLIENT-AAA', result.remediation)
        self.assertIn('Custom Integrations', result.remediation)
        self.assertEqual(result.detail['aps_client_id'], 'CLIENT-AAA')

    def test_an_outage_is_distinguished_from_a_refusal(self):
        """D-6 — a 5xx must never read as "you are not authorised"."""
        with mock.patch.object(probe, '_get', side_effect=_HttpError(503)):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                result = probe.verify_app_authorised(HUB_A)
        self.assertFalse(result.passed)
        self.assertEqual(result.code, 'unavailable')

    def test_a_failure_does_not_advance_the_status(self):
        with mock.patch.object(probe, '_get', side_effect=_HttpError(403)):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                probe.verify_app_authorised(HUB_A)
        self.assertEqual(tenants.get(HUB_A)['onboarding_status'], 'pending_whitelist')

    def test_a_first_time_hub_gets_its_row_created_by_the_pass(self):
        """Regression: step 1 runs before ensure_ssa, so a brand-new hub has no tenant row.
        _mark_app_authorised used to return early on that, so the probe passed and the toast
        fired but nothing persisted — the wizard re-derives its step from onboarding_status
        and sent the admin straight back to step 1. Every other test here pre-creates the
        row in setUp, which is why this went unnoticed."""
        self.assertIsNone(tenants.get(HUB_C))
        with mock.patch.object(probe, '_get', return_value={'data': []}):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                result = probe.verify_app_authorised(HUB_C)
        self.assertTrue(result.passed)
        tenant = tenants.get(HUB_C)
        self.assertIsNotNone(tenant, 'the pass must persist a tenant row')
        self.assertEqual(tenant['onboarding_status'], 'whitelist_verified')
        self.assertIsNotNone(tenant['ssa_verified_at'])
        # Shard pinned to the app whose Client ID was actually verified, so step 2 cannot
        # provision under a different app this hub never whitelisted.
        self.assertEqual(tenant['aps_app_ref'], 'app_a')

    def test_verifying_hub_a_leaves_hub_b_pending(self):
        """TC-WL-06 — whitelisting is per hub and must not be inherited."""
        with mock.patch.object(probe, '_get', return_value={'data': []}):
            with mock.patch('backend.clients.acc.admin_client.get_account_token',
                            return_value='2lo-token'):
                probe.verify_app_authorised(HUB_A)
        self.assertEqual(tenants.get(HUB_B)['onboarding_status'], 'pending_whitelist')


class RobotOnProjectTests(_ProbeTestCase):
    def dc_response(self, status=200):
        return mock.Mock(status_code=status)

    def run_probe(self, hub_id=HUB_A, project_id='b.proj1', *, project_error=None,
                  dc_status=200):
        with mock.patch.object(probe.ssa_token_service, 'get_acc_token',
                               return_value='ssa-token'):
            with mock.patch.object(probe, '_get', side_effect=project_error,
                                   return_value={'data': {}}):
                with mock.patch.object(probe.requests, 'get',
                                       return_value=self.dc_response(dc_status)):
                    with mock.patch(
                        'backend.clients.acc.admin_client.account_id_for_hub',
                        return_value=hub_id.removeprefix('b.'),
                    ):
                        return probe.verify_robot_on_project(hub_id, project_id)

    def test_no_robot_yet_is_reported_as_such(self):
        result = probe.verify_robot_on_project(HUB_A, 'b.proj1')
        self.assertFalse(result.passed)
        self.assertEqual(result.code, 'no_robot')

    def test_both_probes_passing_is_a_pass_naming_the_robot(self):
        self.give_robot()
        result = self.run_probe()
        self.assertTrue(result.passed)
        self.assertEqual(result.detail['robot_email'], ROBOT)

    def test_a_403_on_the_project_names_the_robot_to_invite(self):
        """TC-WL-03 — probe 2 says *which* link is missing."""
        self.give_robot()
        result = self.run_probe(project_error=_HttpError(403))
        self.assertFalse(result.passed)
        self.assertEqual(result.code, 'robot_not_on_project')
        self.assertIn(ROBOT, result.remediation)

    def test_a_404_on_the_project_is_treated_the_same_as_a_403(self):
        self.give_robot()
        result = self.run_probe(project_error=_HttpError(404))
        self.assertEqual(result.code, 'robot_not_on_project')

    def test_a_data_connector_403_names_the_insight_module(self):
        """TC-WL-04 — probe 3 is the one that proves the whole chain."""
        self.give_robot()
        result = self.run_probe(dc_status=403)
        self.assertFalse(result.passed)
        self.assertEqual(result.code, 'robot_missing_data_connector')
        self.assertIn('Insight', result.remediation)
        self.assertIn(ROBOT, result.remediation)

    def test_a_data_connector_outage_is_retryable_not_a_denial(self):
        self.give_robot()
        result = self.run_probe(dc_status=503)
        self.assertEqual(result.code, 'unavailable')

    def test_a_project_outage_is_retryable_not_a_denial(self):
        self.give_robot()
        result = self.run_probe(project_error=_HttpError(500))
        self.assertEqual(result.code, 'unavailable')

    def test_a_token_mint_failure_is_reported_separately(self):
        self.give_robot()
        with mock.patch.object(probe.ssa_token_service, 'get_acc_token',
                               side_effect=RuntimeError('key rejected')):
            result = probe.verify_robot_on_project(HUB_A, 'b.proj1')
        self.assertEqual(result.code, 'token_mint_failed')

    def test_a_second_project_is_probed_again_on_a_verified_hub(self):
        """The FR-03 §8.2 bug this redesign fixes: a hub-level answer would wave the second
        project through and fail at first sync instead of at setup."""
        self.give_robot()
        first = self.run_probe(project_id='b.proj1')
        self.assertTrue(first.passed)
        second = self.run_probe(project_id='b.proj2', project_error=_HttpError(403))
        self.assertFalse(second.passed)
        self.assertEqual(second.code, 'robot_not_on_project')

    def test_the_project_id_is_de_prefixed_for_data_connector(self):
        self.give_robot()
        with mock.patch.object(probe.ssa_token_service, 'get_acc_token',
                               return_value='ssa-token'):
            with mock.patch.object(probe, '_get', return_value={'data': {}}):
                with mock.patch.object(probe.requests, 'get',
                                       return_value=self.dc_response()) as dc_get:
                    with mock.patch(
                        'backend.clients.acc.admin_client.account_id_for_hub',
                        return_value='hub-aaa',
                    ):
                        probe.verify_robot_on_project(HUB_A, 'b.proj1')
        self.assertEqual(dc_get.call_args.kwargs['params']['projectId'], 'proj1')


class ProbeResultTests(unittest.TestCase):
    def test_a_result_serialises_for_the_api(self):
        result = probe.ProbeResult(False, 'no_robot', 'nope', remediation='do a thing')
        self.assertEqual(
            result.as_dict(),
            {'passed': False, 'code': 'no_robot', 'message': 'nope',
             'remediation': 'do a thing', 'detail': {}},
        )


if __name__ == '__main__':
    unittest.main()
