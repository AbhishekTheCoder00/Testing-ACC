"""
Purpose: The portal's HTTP surface is where tenant isolation is actually enforced, so these
tests drive it through Flask's test client rather than calling services directly. The two that
matter most: a project admin is refused (TC-AUTH-02/03/04), and a hub admin cannot reach another
hub by editing the URL (TC-AUTH-05). Also pins that "we could not verify" never renders as
"you are not an admin", and that no secret appears in any response body.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.clients import acc_client  # noqa: E402
from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    ssa_repository as ssa,
    tenant_repository as tenants,
)
from backend.routes import portal_routes  # noqa: E402
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import hub_admin_service, ssa_provisioner  # noqa: E402

HUB_A = {'id': 'b.hub-aaa', 'name': 'Acme West'}
HUB_B = {'id': 'b.hub-bbb', 'name': 'Acme East'}


class _PortalTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/aps-app-a-secret', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-a-secret', 'app-secret')
        hub_admin_service.invalidate()
        self.addCleanup(hub_admin_service.invalidate)

        import app as flask_app

        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()

        self.visible = [HUB_A, HUB_B]
        patcher = mock.patch.object(
            acc_client, 'fetch_hubs', lambda uid: list(self.visible),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        p2 = mock.patch.object(
            portal_routes.acc_client, 'fetch_hubs', lambda uid: list(self.visible),
        )
        p2.start()
        self.addCleanup(p2.stop)

    def sign_in(self, user_id='alice'):
        with self.client.session_transaction() as sess:
            sess['user_id'] = user_id

    def set_admin(self, mapping):
        """mapping: hub_id -> ADMIN | NOT_ADMIN | UNVERIFIED."""
        patcher = mock.patch.object(
            hub_admin_service, 'check_hub_admin',
            lambda user, hub, **kw: mapping.get(hub, hub_admin_service.NOT_ADMIN),
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class PageTests(_PortalTestCase):
    def test_portal_page_is_public(self):
        self.assertEqual(self.client.get('/portal').status_code, 200)

    def test_login_sets_the_return_flag_and_redirects_to_oauth(self):
        resp = self.client.get('/portal/login')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/connect/acc', resp.headers['Location'])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess[portal_routes.POST_LOGIN_KEY], '/portal')

    def test_root_returns_portal_users_to_the_portal(self):
        """The shared ACC callback lands on /; only a portal sign-in is redirected onward."""
        with self.client.session_transaction() as sess:
            sess[portal_routes.POST_LOGIN_KEY] = '/portal'
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/portal'))

    def test_root_is_unchanged_for_u2m_users(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'm2m', resp.data.lower())


class MeTests(_PortalTestCase):
    def test_unauthenticated_is_401(self):
        self.assertEqual(self.client.get('/api/me').status_code, 401)

    def test_admin_sees_only_hubs_they_administer(self):
        """TC-AUTH-05 — Hub B must not appear for a Hub A admin."""
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN,
                        'b.hub-bbb': hub_admin_service.NOT_ADMIN})
        body = self.client.get('/api/me').get_json()
        self.assertEqual(body['access'], 'ok')
        self.assertEqual([h['hub_id'] for h in body['hubs']], ['b.hub-aaa'])

    def test_project_admin_is_denied(self):
        """TC-AUTH-02/03 — definitive refusal, Access Restricted."""
        self.sign_in('casey')
        self.set_admin({})
        body = self.client.get('/api/me').get_json()
        self.assertEqual(body['access'], 'denied')
        self.assertEqual(body['reason'], 'not_hub_admin')
        self.assertEqual(body['hubs'], [])

    def test_user_with_no_hubs_is_denied(self):
        """TC-AUTH-04."""
        self.sign_in()
        self.visible = []
        self.set_admin({})
        body = self.client.get('/api/me').get_json()
        self.assertEqual(body['access'], 'denied')
        self.assertEqual(body['reason'], 'no_hubs')

    def test_unverified_is_not_reported_as_denied(self):
        """The failure worth avoiding: telling a real admin they are not one."""
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.UNVERIFIED,
                        'b.hub-bbb': hub_admin_service.UNVERIFIED})
        with mock.patch.dict(os.environ, {'HUB_ADMIN_ENFORCEMENT': 'strict'}):
            body = self.client.get('/api/me').get_json()
        self.assertEqual(body['access'], 'unverified')
        self.assertEqual(body['aps_client_id'], 'AAA')   # so the UI can show the remedy

    def test_hub_listing_outage_is_retryable_not_a_denial(self):
        self.sign_in()
        with mock.patch.object(portal_routes.acc_client, 'fetch_hubs',
                               side_effect=RuntimeError('APS down')):
            body = self.client.get('/api/me').get_json()
        self.assertEqual(body['access'], 'unavailable')

    def test_no_secret_in_the_response(self):
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN})
        raw = self.client.get('/api/me').data
        self.assertNotIn(b'app-secret', raw)
        self.assertNotIn(b'client_secret', raw)


class HubSelectionTests(_PortalTestCase):
    def test_selecting_a_hub_sets_the_session(self):
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN})
        resp = self.client.post('/api/session/hub', json={'hub_id': 'b.hub-aaa'})
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['tenant_id'], 'b.hub-aaa')

    def test_selecting_a_hub_you_do_not_administer_is_refused(self):
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN,
                        'b.hub-bbb': hub_admin_service.NOT_ADMIN})
        resp = self.client.post('/api/session/hub', json={'hub_id': 'b.hub-bbb'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['reason'], 'not_hub_admin')

    def test_selecting_an_invisible_hub_is_refused(self):
        self.sign_in()
        self.set_admin({'b.other': hub_admin_service.ADMIN})
        resp = self.client.post('/api/session/hub', json={'hub_id': 'b.other'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['reason'], 'not_visible')

    def test_no_tenant_user_row_is_created_for_a_non_admin(self):
        """TC-AUTH-02 — a refused sign-in must leave no trace."""
        self.sign_in('casey')
        self.set_admin({})
        self.client.post('/api/session/hub', json={'hub_id': 'b.hub-aaa'})
        self.assertEqual(tenants.list_users('b.hub-aaa'), [])

    def test_mapping_is_created_once_the_tenant_exists(self):
        """TC-USER-01/02 — second admin joins an onboarded hub."""
        tenants.ensure_tenant('b.hub-aaa', 'app_a')
        self.sign_in('bob')
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN})
        self.client.post('/api/session/hub', json={'hub_id': 'b.hub-aaa'})
        self.assertTrue(tenants.is_member('b.hub-aaa', 'bob'))

    def test_switching_hub_clears_the_connection(self):
        """TC-SESS-06."""
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN,
                        'b.hub-bbb': hub_admin_service.ADMIN})
        with self.client.session_transaction() as sess:
            sess['tenant_id'] = 'b.hub-aaa'
            sess['connection_id'] = 'cnx_old'
        self.client.post('/api/session/hub', json={'hub_id': 'b.hub-bbb'})
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['tenant_id'], 'b.hub-bbb')
            self.assertNotIn('connection_id', sess)

    def test_logout_clears_everything(self):
        self.sign_in()
        self.client.post('/api/session/logout')
        with self.client.session_transaction() as sess:
            self.assertNotIn('user_id', sess)


class OnboardingRouteTests(_PortalTestCase):
    def activate(self, hub_id='b.hub-aaa', user='alice'):
        self.sign_in(user)
        self.set_admin({hub_id: hub_admin_service.ADMIN})
        with self.client.session_transaction() as sess:
            sess['tenant_id'] = hub_id

    def test_status_needs_a_selected_hub(self):
        self.sign_in()
        resp = self.client.get('/api/tenants/b.hub-aaa/ssa/status')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['reason'], 'no_hub')

    def test_cannot_read_another_hub_by_url(self):
        """TC-AUTH-05 / FR-03 §13 — the path hub must match the session hub."""
        self.activate('b.hub-aaa')
        resp = self.client.get('/api/tenants/b.hub-bbb/ssa/status')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['reason'], 'hub_mismatch')

    def test_status_reports_the_client_id_to_whitelist(self):
        self.activate()
        body = self.client.get('/api/tenants/b.hub-aaa/ssa/status').get_json()
        self.assertEqual(body['aps_client_id'], 'AAA')
        self.assertIsNone(body['robot'])
        self.assertEqual(body['capacity'], 'ready')

    def test_status_shows_the_robot_once_provisioned(self):
        self.activate()
        tenants.ensure_tenant('b.hub-aaa', 'app_a')
        ssa.insert(hub_id='b.hub-aaa', aps_app_ref='app_a', service_account_id='svc-1',
                   robot_email='forma-dbx-1@AAA.adskserviceaccount.autodesk.com',
                   key_id='kid-1', private_key_ref='ref/hub/b.hub-aaa/ssa-private-key')
        body = self.client.get('/api/tenants/b.hub-aaa/ssa/status').get_json()
        self.assertIn('adskserviceaccount', body['robot']['email'])
        self.assertNotIn('private_key_ref', body['robot'])   # ref is not the UI's business

    def test_capacity_exhaustion_is_409_not_500(self):
        """Nothing is broken — there is simply no room until ops acts."""
        self.activate()
        with mock.patch.object(
            ssa_provisioner, 'ensure_ssa',
            side_effect=ssa_provisioner.SsaCapacityExhausted('Provisioning capacity reached.'),
        ):
            resp = self.client.post('/api/tenants/b.hub-aaa/ssa/provision')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['reason'], 'capacity_exhausted')

    def test_provision_is_403_for_a_non_admin(self):
        self.activate()
        with mock.patch.object(
            ssa_provisioner, 'ensure_ssa',
            side_effect=hub_admin_service.AccessDenied('nope', reason='not_hub_admin'),
        ):
            resp = self.client.post('/api/tenants/b.hub-aaa/ssa/provision')
        self.assertEqual(resp.status_code, 403)

    def test_provision_returns_the_robot(self):
        self.activate()

        def fake_ensure(hub_id, aps_user_id=None):
            tenants.ensure_tenant(hub_id, 'app_a')
            return ssa.insert(
                hub_id=hub_id, aps_app_ref='app_a', service_account_id='svc-1',
                robot_email='forma-dbx-1@AAA.adskserviceaccount.autodesk.com',
                key_id='kid-1', private_key_ref='ref/x',
            )

        with mock.patch.object(ssa_provisioner, 'ensure_ssa', fake_ensure):
            body = self.client.post('/api/tenants/b.hub-aaa/ssa/provision').get_json()
        self.assertIn('adskserviceaccount', body['robot']['email'])


class Test_U2M_Flag(_PortalTestCase):
    """ENABLE_U2M=false turns the portal into the front door and takes the wizard off the air.

    The routes the portal borrows from the U2M blueprints (sign-in, project picker, catalog
    picker) must survive the block — that is the failure mode worth pinning.
    """

    WIZARD_ONLY = ['/state', '/reset', '/hubs', '/folders', '/admin-projects',
                   '/bootstrap/status', '/dashboard/data', '/connect/databricks/status',
                   '/sync/status', '/sync/auto', '/dc/requests']
    SHARED = ['/portal', '/projects', '/databricks/catalogs', '/health']

    def u2m(self, enabled):
        p = mock.patch.dict(os.environ, {'ENABLE_U2M': 'true' if enabled else 'false'})
        p.start()
        self.addCleanup(p.stop)

    def test_root_serves_the_wizard_by_default(self):
        self.u2m(True)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_wizard_routes_stay_up_by_default(self):
        self.u2m(True)
        for path in self.WIZARD_ONLY:
            self.assertNotEqual(self.client.get(path).status_code, 404, path)

    def test_root_redirects_to_the_portal_when_disabled(self):
        self.u2m(False)
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/portal'))

    def test_wizard_routes_are_blocked_when_disabled(self):
        self.u2m(False)
        self.sign_in()
        for path in self.WIZARD_ONLY:
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_portal_routes_survive_the_block(self):
        self.u2m(False)
        self.sign_in()
        self.set_admin({'b.hub-aaa': hub_admin_service.ADMIN})
        for path in self.SHARED + ['/api/me', '/api/connections']:
            self.assertNotEqual(self.client.get(path).status_code, 404, path)

    def test_acc_sign_in_survives_the_block(self):
        """/portal/login redirects into the U2M blueprint's /connect/acc — the one route the
        portal cannot lose."""
        self.u2m(False)
        with mock.patch.dict(os.environ, {'APS_CLIENT_ID': 'test-client'}):
            resp = self.client.get('/connect/acc')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('test-client', resp.headers['Location'])

    def test_portal_oauth_return_still_wins_over_the_redirect(self):
        """The ACC callback lands on `/`; the portal's post-login flag must still be honoured."""
        self.u2m(False)
        with self.client.session_transaction() as sess:
            sess[portal_routes.POST_LOGIN_KEY] = '/portal?dbx=1'
        resp = self.client.get('/')
        self.assertTrue(resp.headers['Location'].endswith('/portal?dbx=1'))


if __name__ == '__main__':
    unittest.main()
