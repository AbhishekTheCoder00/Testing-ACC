"""
Purpose: The onboarding API is where a hub admin is told what to do next, so these tests pin
the status codes and the copy that distinguishes the failure modes: capacity exhaustion is a
409 with the cs-16 status (nothing is broken, there is no room), a permissions refusal is 403,
and an APS outage is 502. Also pins that no private key or app secret appears in any body, and
that a hub in the URL cannot differ from the session's. Traces FR-03 §13, FR-04 §7, TC-SSA-*.
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
from backend.routes import onboarding_routes  # noqa: E402
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import hub_admin_service, ssa_provisioner  # noqa: E402
from backend.services.m2m.provisioning_probe import ProbeResult  # noqa: E402

HUB = 'b.hub-aaa'
OTHER_HUB = 'b.hub-bbb'
ROBOT = 'forma-dbx-aaa@autodesk-svc.com'
PEM = '-----BEGIN PRIVATE KEY-----\nsupersecretkeymaterial\n-----END PRIVATE KEY-----'


class _OnboardingTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'CLIENT-AAA', 'ref/aps-app-a', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-a', 'app-secret')
        tenants.ensure_tenant(HUB, 'app_a')
        tenants.ensure_tenant(OTHER_HUB, 'app_a')

        import app as flask_app

        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()

    def sign_in(self, user_id='alice', hub_id=HUB):
        with self.client.session_transaction() as sess:
            sess['user_id'] = user_id
            if hub_id:
                sess['tenant_id'] = hub_id

    def give_robot(self, hub_id=HUB):
        get_secret_store().put_secret(f'ref/{hub_id}/key', PEM)
        return ssa.insert(
            hub_id=hub_id, aps_app_ref='app_a', service_account_id=f'sa-{hub_id}',
            robot_email=ROBOT, key_id='key-1', private_key_ref=f'ref/{hub_id}/key',
        )


class StatusTests(_OnboardingTestCase):
    def test_status_needs_a_signed_in_user(self):
        self.assertEqual(
            self.client.get(f'/api/tenants/{HUB}/ssa/status').status_code, 401,
        )

    def test_a_hub_in_the_url_must_match_the_session(self):
        """FR-03 §13 — editing the URL must not act on another hub."""
        self.sign_in()
        resp = self.client.get(f'/api/tenants/{OTHER_HUB}/ssa/status')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['reason'], 'hub_mismatch')

    def test_status_carries_the_client_id_the_admin_must_whitelist(self):
        """FR-04 §7 — resolved per hub, so it stays right if a second app is registered."""
        self.sign_in()
        body = self.client.get(f'/api/tenants/{HUB}/ssa/status').get_json()
        self.assertEqual(body['aps_client_id'], 'CLIENT-AAA')
        self.assertEqual(body['onboarding_status'], 'pending_whitelist')
        self.assertIsNone(body['robot'])
        self.assertEqual(body['slots_remaining'], 10)

    def test_status_reports_the_robot_without_its_key(self):
        """NFR-01 — the key material must never leave the vault."""
        self.give_robot()
        self.sign_in()
        resp = self.client.get(f'/api/tenants/{HUB}/ssa/status')
        raw = resp.get_data(as_text=True)
        self.assertEqual(resp.get_json()['robot']['email'], ROBOT)
        self.assertNotIn('supersecretkeymaterial', raw)
        self.assertNotIn('private_key_ref', raw)
        self.assertNotIn('app-secret', raw)


class ProvisionTests(_OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def test_provisioning_returns_the_status_payload(self):
        with mock.patch.object(ssa_provisioner, 'ensure_ssa',
                               side_effect=lambda hub, **kw: self.give_robot(hub)):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/provision')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['robot']['email'], ROBOT)

    def test_the_signed_in_user_is_passed_through_for_the_admin_check(self):
        with mock.patch.object(ssa_provisioner, 'ensure_ssa') as ensure:
            self.client.post(f'/api/tenants/{HUB}/ssa/provision')
        ensure.assert_called_once_with(HUB, aps_user_id='alice')

    def test_capacity_exhaustion_is_a_409_with_the_limit_status(self):
        """FR-06 cs-16 — nothing is broken, there is simply no room until ops acts."""
        with mock.patch.object(
            ssa_provisioner, 'ensure_ssa',
            side_effect=ssa_provisioner.SsaCapacityExhausted('No robot slots left.'),
        ):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/provision')
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body['reason'], 'capacity_exhausted')
        self.assertEqual(body['onboarding_status'], 'ssa_limit_reached')

    def test_a_permissions_refusal_is_a_403(self):
        with mock.patch.object(
            ssa_provisioner, 'ensure_ssa',
            side_effect=hub_admin_service.AccessDenied(
                'You are not an admin of this hub.', reason='not_admin',
            ),
        ):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/provision')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['reason'], 'not_admin')

    def test_an_aps_failure_is_a_502_not_a_500(self):
        with mock.patch.object(
            ssa_provisioner, 'ensure_ssa',
            side_effect=ssa_provisioner.SsaProvisionError('APS refused the create'),
        ):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/provision')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['reason'], 'provision_failed')


class VerifyTests(_OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def test_hub_verification_runs_probe_one(self):
        passing = ProbeResult(True, 'ok', 'Custom Integration verified.')
        with mock.patch.object(onboarding_routes.provisioning_probe,
                               'verify_app_authorised', return_value=passing) as p:
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/verify')
        p.assert_called_once_with(HUB)
        body = resp.get_json()
        self.assertTrue(body['passed'])
        # Status is merged in so the wizard needs one call, not two.
        self.assertEqual(body['aps_client_id'], 'CLIENT-AAA')

    def test_a_failed_hub_verification_returns_200_with_the_remediation(self):
        """A probe answering "not done yet" is a successful question, not an HTTP error."""
        failing = ProbeResult(
            False, 'client_id_not_whitelisted', 'Autodesk refused this request.',
            remediation='Add Client ID CLIENT-AAA in Custom Integrations.',
        )
        with mock.patch.object(onboarding_routes.provisioning_probe,
                               'verify_app_authorised', return_value=failing):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/verify')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['passed'])
        self.assertIn('CLIENT-AAA', resp.get_json()['remediation'])

    def test_a_passing_verification_clears_the_stale_hub_admin_answer(self):
        """Regression: the account-admin probe is what a missing whitelist breaks, and its
        answer is cached for 15 minutes. Without invalidation the admin whitelists us, passes
        this check, and is still told "You are not an administrator of this hub" until the
        entry ages out — the exact "I am the admin but it says I am not" report."""
        self.addCleanup(hub_admin_service.invalidate)  # module-global cache
        hub_admin_service._remember('alice', HUB, hub_admin_service.NOT_ADMIN)
        self.assertEqual(hub_admin_service._cached('alice', HUB),
                         hub_admin_service.NOT_ADMIN)
        passing = ProbeResult(True, 'ok', 'Custom Integration verified.')
        with mock.patch.object(onboarding_routes.provisioning_probe,
                               'verify_app_authorised', return_value=passing):
            self.client.post(f'/api/tenants/{HUB}/ssa/verify')
        self.assertIsNone(hub_admin_service._cached('alice', HUB),
                          'a passing whitelist check must re-open the admin question')

    def test_a_failing_verification_leaves_the_cache_alone(self):
        """Nothing changed, so do not pay an APS round trip per hub on every failed retry."""
        self.addCleanup(hub_admin_service.invalidate)  # module-global cache
        hub_admin_service._remember('alice', HUB, hub_admin_service.NOT_ADMIN)
        failing = ProbeResult(False, 'client_id_not_whitelisted', 'Autodesk refused.')
        with mock.patch.object(onboarding_routes.provisioning_probe,
                               'verify_app_authorised', return_value=failing):
            self.client.post(f'/api/tenants/{HUB}/ssa/verify')
        self.assertEqual(hub_admin_service._cached('alice', HUB),
                         hub_admin_service.NOT_ADMIN)

    def test_project_verification_runs_probes_two_and_three(self):
        passing = ProbeResult(True, 'ok', 'Service account verified on this project.')
        with mock.patch.object(onboarding_routes.provisioning_probe,
                               'verify_robot_on_project', return_value=passing) as p:
            resp = self.client.post(f'/api/tenants/{HUB}/projects/b.proj1/verify')
        p.assert_called_once_with(HUB, 'b.proj1')
        self.assertTrue(resp.get_json()['passed'])
        self.assertEqual(resp.get_json()['project_id'], 'b.proj1')

    def test_project_verification_is_hub_scoped(self):
        self.assertEqual(
            self.client.post(
                f'/api/tenants/{OTHER_HUB}/projects/b.proj1/verify',
            ).status_code,
            403,
        )


class RotateKeyTests(_OnboardingTestCase):
    def setUp(self):
        super().setUp()
        self.give_robot()
        self.sign_in()

    def test_rotation_returns_the_new_status_without_the_key(self):
        with mock.patch.object(ssa_provisioner, 'rotate_key') as rotate:
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/rotate-key')
        rotate.assert_called_once_with(HUB)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('supersecretkeymaterial', resp.get_data(as_text=True))

    def test_a_failed_rotation_is_a_400_with_a_reason(self):
        with mock.patch.object(
            ssa_provisioner, 'rotate_key',
            side_effect=ssa_provisioner.SsaProvisionError('APS refused the new key'),
        ):
            resp = self.client.post(f'/api/tenants/{HUB}/ssa/rotate-key')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['reason'], 'rotate_failed')


if __name__ == '__main__':
    unittest.main()
