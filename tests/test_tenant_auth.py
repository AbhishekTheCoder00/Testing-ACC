"""
Purpose: tenant_auth is where cross-tenant access is actually refused, so these tests exercise
the decision, not the callers: an unmapped user cannot act on a hub, a connection belonging to
another hub reads as 404 rather than 403 (so the response cannot be used to enumerate ids), and
switching hub clears the connection context. Membership is re-read per request on purpose —
removing an admin must take effect immediately, not at session expiry. Traces TC-SESS-01…06,
TC-AUTH-05, TC-NFR-02.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    tenant_repository as tenants,
)
from backend.services.m2m.connection_service import create_or_resume  # noqa: E402
from backend.utils import tenant_auth  # noqa: E402

HUB = 'b.hub-aaa'
OTHER_HUB = 'b.hub-bbb'
WS = 'https://dbc-abc123.cloud.databricks.com'


class _AuthTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        tenants.ensure_tenant(OTHER_HUB, 'app_a')
        tenants.add_user(HUB, 'alice')
        tenants.add_user(OTHER_HUB, 'bob')

        self.app = Flask(__name__)
        self.app.secret_key = 'unit-test'

    def ctx(self, **session_values):
        """A request context with a pre-populated session."""
        ctx = self.app.test_request_context('/')
        ctx.push()
        self.addCleanup(ctx.pop)
        from flask import session

        session.update(session_values)
        return ctx


class RequireUserTests(_AuthTestCase):
    def test_no_session_is_a_401(self):
        self.ctx()
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_user()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.reason, 'not_authenticated')

    def test_a_signed_in_user_is_returned(self):
        self.ctx(user_id='alice')
        self.assertEqual(tenant_auth.require_user(), 'alice')


class RequireHubTests(_AuthTestCase):
    def test_no_hub_selected_is_a_400(self):
        self.ctx(user_id='alice')
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_active_hub()
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.reason, 'no_hub')

    def test_an_active_hub_needs_no_membership_row(self):
        """Onboarding runs before the tenant exists — demanding membership here would lock a
        brand-new hub out of its own setup."""
        self.ctx(user_id='carol', tenant_id='b.brand-new')
        self.assertEqual(tenant_auth.require_active_hub(), ('carol', 'b.brand-new'))

    def test_require_tenant_refuses_a_non_member(self):
        self.ctx(user_id='carol', tenant_id=HUB)
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_tenant()
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.reason, 'not_a_member')

    def test_require_tenant_admits_a_member(self):
        self.ctx(user_id='alice', tenant_id=HUB)
        self.assertEqual(tenant_auth.require_tenant(), ('alice', HUB))

    def test_membership_is_reread_every_request(self):
        """Removing an admin takes effect on their next request, not at session expiry."""
        from backend.repositories.state.database import _conn

        self.ctx(user_id='alice', tenant_id=HUB)
        self.assertEqual(tenant_auth.require_tenant(), ('alice', HUB))
        with _conn() as con:
            con.execute(
                'DELETE FROM tenant_users WHERE tenant_id = ? AND aps_user_id = ?',
                (HUB, 'alice'),
            )
        with self.assertRaises(tenant_auth.AuthError):
            tenant_auth.require_tenant()

    def test_a_hub_in_the_url_must_match_the_session(self):
        self.ctx(user_id='alice', tenant_id=HUB)
        self.assertEqual(tenant_auth.require_hub(HUB), 'alice')
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_hub(OTHER_HUB)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.reason, 'hub_mismatch')


class RequireConnectionTests(_AuthTestCase):
    def setUp(self):
        super().setUp()
        self.mine = create_or_resume(
            hub_id=HUB, project_id='p1', dbx_workspace_url=WS, catalog='c1',
        ).connection
        self.theirs = create_or_resume(
            hub_id=OTHER_HUB, project_id='p9', dbx_workspace_url=WS, catalog='c9',
        ).connection

    def test_own_connection_is_loaded(self):
        self.ctx(user_id='alice', tenant_id=HUB)
        loaded = tenant_auth.require_connection(self.mine['connection_id'])
        self.assertEqual(loaded['connection_id'], self.mine['connection_id'])

    def test_another_hubs_connection_is_404_not_403(self):
        """TC-AUTH-05 — the response must not confirm that the id exists."""
        self.ctx(user_id='alice', tenant_id=HUB)
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_connection(self.theirs['connection_id'])
        self.assertEqual(caught.exception.status, 404)

    def test_a_tampered_id_is_404(self):
        self.ctx(user_id='alice', tenant_id=HUB)
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_connection('cnx_deadbeef')
        self.assertEqual(caught.exception.status, 404)

    def test_a_non_member_cannot_load_even_a_real_connection(self):
        self.ctx(user_id='carol', tenant_id=HUB)
        with self.assertRaises(tenant_auth.AuthError) as caught:
            tenant_auth.require_connection(self.mine['connection_id'])
        self.assertEqual(caught.exception.status, 403)


class SessionSwitchTests(_AuthTestCase):
    def test_switching_hub_clears_the_connection(self):
        """TC-SESS-06 — nothing may leak across a hub switch."""
        from flask import session

        self.ctx(user_id='alice', tenant_id=HUB, connection_id='cnx_something')
        tenant_auth.set_active_hub(OTHER_HUB)
        self.assertEqual(session['tenant_id'], OTHER_HUB)
        self.assertNotIn('connection_id', session)

    def test_clearing_context_leaves_the_user_signed_in(self):
        from flask import session

        self.ctx(user_id='alice', tenant_id=HUB, connection_id='cnx_x')
        tenant_auth.clear_tenant_context()
        self.assertEqual(session['user_id'], 'alice')
        self.assertNotIn('tenant_id', session)


class TenantRouteTests(_AuthTestCase):
    def test_an_auth_error_becomes_json_with_its_status(self):
        @tenant_auth.tenant_route
        def route():
            raise tenant_auth.AuthError(403, 'Nope.', reason='not_a_member')

        self.ctx(user_id='alice')
        body, status = route()
        self.assertEqual(status, 403)
        self.assertEqual(body.get_json(), {'error': 'Nope.', 'reason': 'not_a_member'})

    def test_other_exceptions_are_not_swallowed(self):
        @tenant_auth.tenant_route
        def route():
            raise RuntimeError('a real bug')

        self.ctx(user_id='alice')
        with self.assertRaises(RuntimeError):
            route()


if __name__ == '__main__':
    unittest.main()
