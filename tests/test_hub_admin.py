"""
Purpose: Hub-admin-only access (FR-02 FR-01a, BR-12, BO-08) hinges on telling three outcomes
apart: the user IS an admin, the user is NOT, and we CANNOT TELL because the Client ID is not
whitelisted yet or APS is down. Conflating the last two either locks every new customer out or
tells a real admin they are not one. Traces TC-AUTH-01/02/03/04/08.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')

from backend.clients.acc import admin_client  # noqa: E402
from backend.services.m2m import hub_admin_service as svc  # noqa: E402

HUB_A = {'id': 'b.hub-aaa', 'name': 'Acme West'}
HUB_B = {'id': 'b.hub-bbb', 'name': 'Acme East'}
CREDS = {'client_id': 'AAA', 'client_secret': 'sekrit'}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.ok = status_code < 400

    def json(self):
        return self._payload


class _AdminTestCase(unittest.TestCase):
    def setUp(self):
        svc.invalidate()
        admin_client.invalidate_token_cache()
        self.addCleanup(svc.invalidate)
        self.addCleanup(admin_client.invalidate_token_cache)
        env = mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'provisional'})
        env.start()
        self.addCleanup(env.stop)

    def patch_probe(self, per_hub: dict):
        """per_hub maps account_id -> 'admin' | 'not_admin' | Exception."""
        self.probe_calls = []

        def fake_token(client_id, client_secret):
            return 'account-tok'

        def fake_is_admin(token, account_id, aps_user_id):
            self.probe_calls.append((account_id, aps_user_id))
            outcome = per_hub.get(account_id, 'not_admin')
            if isinstance(outcome, Exception):
                raise outcome
            return outcome == 'admin'

        for name, impl in (('get_account_token', fake_token),
                           ('is_account_admin', fake_is_admin)):
            patcher = mock.patch.object(admin_client, name, impl)
            patcher.start()
            self.addCleanup(patcher.stop)


class AccountIdTests(unittest.TestCase):
    def test_strips_the_b_prefix(self):
        self.assertEqual(admin_client.account_id_for_hub('b.7f3a'), '7f3a')

    def test_leaves_a_bare_account_id_alone(self):
        self.assertEqual(admin_client.account_id_for_hub('7f3a'), '7f3a')


class FindAccountUserTests(unittest.TestCase):
    def setUp(self):
        admin_client.invalidate_token_cache()
        self.addCleanup(admin_client.invalidate_token_cache)

    def patch_requests(self, *responses):
        fake = mock.MagicMock()
        fake.get.side_effect = list(responses)
        fake.RequestException = Exception
        patcher = mock.patch.object(admin_client, 'requests', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_matches_on_uid_not_id(self):
        """OAuth gives us the Autodesk uid; `id` is an ACC-internal identifier."""
        self.patch_requests(FakeResponse(200, [
            {'id': 'acc-1', 'uid': 'OTHER', 'role': 'account_admin'},
            {'id': 'acc-2', 'uid': 'ME', 'role': 'account_user'},
        ]))
        user = admin_client.find_account_user('tok', 'acct', 'ME')
        self.assertEqual(user['id'], 'acc-2')

    def test_is_account_admin_true(self):
        self.patch_requests(FakeResponse(200, [{'uid': 'ME', 'role': 'account_admin'}]))
        self.assertTrue(admin_client.is_account_admin('tok', 'acct', 'ME'))

    def test_is_account_admin_false_for_other_roles(self):
        """TC-AUTH-02/03 — a project admin or viewer is definitively not a hub admin."""
        self.patch_requests(FakeResponse(200, [{'uid': 'ME', 'role': 'project_admin'}]))
        self.assertFalse(admin_client.is_account_admin('tok', 'acct', 'ME'))

    def test_user_absent_from_account_is_definitively_not_admin(self):
        self.patch_requests(FakeResponse(200, []))
        self.assertFalse(admin_client.is_account_admin('tok', 'acct', 'ME'))

    def test_403_is_unavailable_not_a_denial(self):
        """403 means the Client ID is not whitelisted yet, not that the user lacks the role."""
        self.patch_requests(FakeResponse(403, {}))
        with self.assertRaises(admin_client.AccountAdminUnavailable):
            admin_client.is_account_admin('tok', 'acct', 'ME')

    def test_500_is_unavailable(self):
        self.patch_requests(FakeResponse(500, {}))
        with self.assertRaises(admin_client.AccountAdminUnavailable):
            admin_client.is_account_admin('tok', 'acct', 'ME')

    def test_paginates_until_a_short_page(self):
        page1 = [{'uid': f'u{i}', 'role': 'account_user'} for i in range(100)]
        page2 = [{'uid': 'ME', 'role': 'account_admin'}]
        fake = self.patch_requests(FakeResponse(200, page1), FakeResponse(200, page2))
        self.assertTrue(admin_client.is_account_admin('tok', 'acct', 'ME'))
        self.assertEqual(fake.get.call_count, 2)

    def test_wrapped_data_payload_is_handled(self):
        self.patch_requests(FakeResponse(200, {'data': [{'uid': 'ME', 'role': 'account_admin'}]}))
        self.assertTrue(admin_client.is_account_admin('tok', 'acct', 'ME'))


class CheckHubAdminTests(_AdminTestCase):
    def test_admin(self):
        self.patch_probe({'hub-aaa': 'admin'})
        self.assertEqual(svc.check_hub_admin('me', 'b.hub-aaa', **CREDS), svc.ADMIN)

    def test_not_admin(self):
        self.patch_probe({'hub-aaa': 'not_admin'})
        self.assertEqual(svc.check_hub_admin('me', 'b.hub-aaa', **CREDS), svc.NOT_ADMIN)

    def test_unavailable_is_unverified(self):
        self.patch_probe({'hub-aaa': admin_client.AccountAdminUnavailable('403')})
        self.assertEqual(svc.check_hub_admin('me', 'b.hub-aaa', **CREDS), svc.UNVERIFIED)

    def test_result_is_cached(self):
        self.patch_probe({'hub-aaa': 'admin'})
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        self.assertEqual(len(self.probe_calls), 1)

    def test_cache_is_per_user_and_hub(self):
        self.patch_probe({'hub-aaa': 'admin', 'hub-bbb': 'admin'})
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        svc.check_hub_admin('me', 'b.hub-bbb', **CREDS)
        svc.check_hub_admin('you', 'b.hub-aaa', **CREDS)
        self.assertEqual(len(self.probe_calls), 3)

    def test_invalidate_forces_a_reprobe(self):
        self.patch_probe({'hub-aaa': 'admin'})
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        svc.invalidate('me')
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        self.assertEqual(len(self.probe_calls), 2)

    def test_permissive_short_circuits_without_probing(self):
        self.patch_probe({'hub-aaa': 'not_admin'})
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'permissive'}):
            self.assertEqual(svc.check_hub_admin('me', 'b.hub-aaa', **CREDS), svc.ADMIN)
        self.assertEqual(self.probe_calls, [])

    def test_unknown_enforcement_mode_is_rejected(self):
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'sometimes'}):
            with self.assertRaises(ValueError):
                svc.enforcement_mode()

    def test_default_is_strict(self):
        """Whitelisting the Client ID is a documented prerequisite before first sign-in,
        so by the time anyone reaches us the probe is answerable and we can be strict."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HUB_ADMIN_ENFORCEMENT', None)
            self.assertEqual(svc.enforcement_mode(), svc.ENFORCEMENT_STRICT)


class AdminHubsTests(_AdminTestCase):
    def test_admin_hub_is_returned(self):
        """TC-AUTH-01."""
        self.patch_probe({'hub-aaa': 'admin'})
        hubs = svc.admin_hubs('me', [HUB_A], **CREDS)
        self.assertEqual(hubs[0]['id'], 'b.hub-aaa')
        self.assertEqual(hubs[0]['admin_status'], svc.ADMIN)

    def test_non_admin_is_denied(self):
        """TC-AUTH-02 / TC-AUTH-03 — project admin and viewer get Access Restricted."""
        self.patch_probe({'hub-aaa': 'not_admin'})
        with self.assertRaises(svc.AccessDenied) as ctx:
            svc.admin_hubs('me', [HUB_A], **CREDS)
        self.assertEqual(ctx.exception.reason, 'not_hub_admin')

    def test_user_with_no_hubs_at_all_is_denied(self):
        """TC-AUTH-04."""
        self.patch_probe({})
        with self.assertRaises(svc.AccessDenied) as ctx:
            svc.admin_hubs('me', [], **CREDS)
        self.assertEqual(ctx.exception.reason, 'no_hubs')

    def test_non_admin_hubs_are_filtered_out(self):
        """TC-AUTH-05 — Hub B must not appear for someone who only admins Hub A."""
        self.patch_probe({'hub-aaa': 'admin', 'hub-bbb': 'not_admin'})
        hubs = svc.admin_hubs('me', [HUB_A, HUB_B], **CREDS)
        self.assertEqual([h['id'] for h in hubs], ['b.hub-aaa'])

    def test_provisional_keeps_unverified_hubs_so_onboarding_can_start(self):
        """A brand-new hub always probes 403 — denying here would block every new customer."""
        self.patch_probe({'hub-aaa': admin_client.AccountAdminUnavailable('403')})
        hubs = svc.admin_hubs('me', [HUB_A], **CREDS)
        self.assertEqual(hubs[0]['admin_status'], svc.UNVERIFIED)

    def test_strict_rejects_unverified_with_a_distinct_reason(self):
        self.patch_probe({'hub-aaa': admin_client.AccountAdminUnavailable('503')})
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}):
            with self.assertRaises(svc.AccessDenied) as ctx:
                svc.admin_hubs('me', [HUB_A], **CREDS)
        self.assertEqual(ctx.exception.reason, 'unverified')

    def test_strict_still_admits_a_confirmed_admin(self):
        self.patch_probe({'hub-aaa': 'admin',
                          'hub-bbb': admin_client.AccountAdminUnavailable('403')})
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}):
            hubs = svc.admin_hubs('me', [HUB_A, HUB_B], **CREDS)
        self.assertEqual([h['id'] for h in hubs], ['b.hub-aaa'])


class RequireConclusiveAdminTests(_AdminTestCase):
    def test_admin_passes(self):
        self.patch_probe({'hub-aaa': 'admin'})
        svc.require_conclusive_admin('me', 'b.hub-aaa', **CREDS)

    def test_non_admin_is_blocked_before_a_robot_is_created(self):
        """The provisional sign-in must not let a non-admin burn a service-account slot."""
        self.patch_probe({'hub-aaa': 'not_admin'})
        with self.assertRaises(svc.AccessDenied) as ctx:
            svc.require_conclusive_admin('me', 'b.hub-aaa', **CREDS)
        self.assertEqual(ctx.exception.reason, 'not_hub_admin')

    def test_unverified_is_blocked_and_points_at_the_whitelist(self):
        self.patch_probe({'hub-aaa': admin_client.AccountAdminUnavailable('403')})
        with self.assertRaises(svc.AccessDenied) as ctx:
            svc.require_conclusive_admin('me', 'b.hub-aaa', **CREDS)
        self.assertEqual(ctx.exception.reason, 'unverified')
        self.assertIn('Custom Integrations', str(ctx.exception))

    def test_bypasses_the_cache(self):
        """Whitelisting flips the answer; a 15-minute stale 'unverified' must not persist."""
        self.patch_probe({'hub-aaa': 'admin'})
        svc.check_hub_admin('me', 'b.hub-aaa', **CREDS)
        svc.require_conclusive_admin('me', 'b.hub-aaa', **CREDS)
        self.assertEqual(len(self.probe_calls), 2)


if __name__ == '__main__':
    unittest.main()
