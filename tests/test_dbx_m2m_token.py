"""
Purpose: Scheduled sync must reach Databricks as a service principal and never as a human
(FR-02 FR-26, BR-08). These tests pin that the mint uses client_credentials with HTTP Basic
and no refresh-token grant ever appears, that tokens are cached per connection and re-minted
near expiry, and that a 401/403 triggers exactly one forced re-mint and retry — and nothing
else does. Traces TC-SYNC-03, FR-03 §10.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.clients.dbx import m2m_auth_client  # noqa: E402
from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    connection_repository as conns,
    tenant_repository as tenants,
)
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import dbx_token_service  # noqa: E402
from backend.services.m2m.connection_identity import compute_connection_id  # noqa: E402

WS = 'https://dbc-abc123.cloud.databricks.com'


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self.reason = 'OK' if status_code < 400 else 'Error'
        self._payload = payload or {}
        self.text = text or str(self._payload)
        self.ok = status_code < 400

    def json(self):
        return self._payload


class FakeRequests:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(200, {'access_token': 'tok', 'expires_in': 3600})


class MintTests(unittest.TestCase):
    def patch(self, *responses):
        fake = FakeRequests(*responses)
        patcher = mock.patch.object(m2m_auth_client, 'requests', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_posts_client_credentials_to_the_oidc_endpoint(self):
        fake = self.patch(FakeResponse(200, {'access_token': 'sp-tok', 'expires_in': 3600}))
        resp = m2m_auth_client.mint_workspace_token(WS, 'sp-id', 'sp-secret')
        self.assertEqual(resp.access_token, 'sp-tok')

        url, kwargs = fake.calls[0]
        self.assertEqual(url, f'{WS}/oidc/v1/token')
        self.assertEqual(kwargs['data']['grant_type'], 'client_credentials')
        self.assertEqual(kwargs['data']['scope'], 'all-apis')

    def test_uses_basic_auth(self):
        fake = self.patch(FakeResponse(200, {'access_token': 't', 'expires_in': 3600}))
        m2m_auth_client.mint_workspace_token(WS, 'sp-id', 'sp-secret')
        self.assertEqual(fake.calls[0][1]['auth'], ('sp-id', 'sp-secret'))

    def test_never_requests_offline_access(self):
        """FR-03 §10.2 — there is no refresh_token grant on this path."""
        fake = self.patch(FakeResponse(200, {'access_token': 't', 'expires_in': 3600}))
        m2m_auth_client.mint_workspace_token(WS, 'sp-id', 'sp-secret')
        body = fake.calls[0][1]['data']
        self.assertNotIn('offline_access', body['scope'])
        self.assertNotIn('refresh_token', body)

    def test_trailing_slash_on_workspace_is_normalised(self):
        fake = self.patch(FakeResponse(200, {'access_token': 't', 'expires_in': 3600}))
        m2m_auth_client.mint_workspace_token(WS + '/', 'sp-id', 'sp-secret')
        self.assertEqual(fake.calls[0][0], f'{WS}/oidc/v1/token')

    def test_error_does_not_leak_the_secret(self):
        self.patch(FakeResponse(401, {}, text='invalid_client sp-secret'))
        with self.assertRaises(m2m_auth_client.DbxAuthError) as ctx:
            m2m_auth_client.mint_workspace_token(WS, 'sp-id', 'sp-secret')
        self.assertNotIn('sp-secret', str(ctx.exception))

    def test_missing_token_in_response_is_an_error(self):
        self.patch(FakeResponse(200, {'expires_in': 3600}))
        with self.assertRaises(m2m_auth_client.DbxAuthError):
            m2m_auth_client.mint_workspace_token(WS, 'sp-id', 'sp-secret')


class TokenServiceTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant('b.hub1', 'app_a')
        self.cid = compute_connection_id('b.hub1', 'p1', WS, 'c1')
        conns.insert(
            connection_id=self.cid, hub_id='b.hub1', project_id='p1',
            dbx_workspace_url=WS, catalog='c1',
        )
        get_secret_store().put_secret('ref/sp', 'sp-secret')
        conns.save_dbx_credentials(
            self.cid, workspace_url=WS, client_id='sp-id', client_secret_ref='ref/sp',
        )
        dbx_token_service.invalidate()
        self.addCleanup(dbx_token_service.invalidate)

    def patch_mint(self, *tokens):
        calls = []

        def fake_mint(workspace_url, client_id, client_secret, **kw):
            calls.append((workspace_url, client_id, client_secret))
            token, expires = tokens[min(len(calls) - 1, len(tokens) - 1)]
            return m2m_auth_client.TokenResponse(access_token=token, expires_in=expires)

        patcher = mock.patch.object(dbx_token_service, 'mint_workspace_token', fake_mint)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_reads_the_secret_from_the_store_not_the_database(self):
        calls = self.patch_mint(('tok-1', 3600))
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(calls[0], (WS, 'sp-id', 'sp-secret'))

    def test_token_is_cached_per_connection(self):
        calls = self.patch_mint(('tok-1', 3600), ('tok-2', 3600))
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(len(calls), 1)

    def test_near_expiry_is_reminted(self):
        calls = self.patch_mint(('tok-1', 5), ('tok-2', 3600))
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_force_bypasses_the_cache(self):
        calls = self.patch_mint(('tok-1', 3600), ('tok-2', 3600))
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid, force=True), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_invalidate_clears_one_connection(self):
        calls = self.patch_mint(('tok-1', 3600), ('tok-2', 3600))
        dbx_token_service.get_dbx_token(self.cid)
        dbx_token_service.invalidate(self.cid)
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_two_connections_cache_independently(self):
        other = compute_connection_id('b.hub1', 'p2', WS, 'c2')
        conns.insert(connection_id=other, hub_id='b.hub1', project_id='p2',
                     dbx_workspace_url=WS, catalog='c2')
        get_secret_store().put_secret('ref/sp2', 'other-secret')
        conns.save_dbx_credentials(other, workspace_url=WS, client_id='sp-2',
                                   client_secret_ref='ref/sp2')
        calls = self.patch_mint(('tok-1', 3600), ('tok-2', 3600))
        self.assertEqual(dbx_token_service.get_dbx_token(self.cid), 'tok-1')
        self.assertEqual(dbx_token_service.get_dbx_token(other), 'tok-2')
        self.assertEqual(calls[1][1], 'sp-2')

    def test_missing_credentials_raises_a_clear_error(self):
        """D-4 — a connection with no SP cannot run headlessly, and must say so."""
        bare = compute_connection_id('b.hub1', 'p3', WS, 'c3')
        conns.insert(connection_id=bare, hub_id='b.hub1', project_id='p3',
                     dbx_workspace_url=WS, catalog='c3')
        with self.assertRaises(dbx_token_service.MissingServicePrincipal):
            dbx_token_service.get_dbx_token(bare)

    def test_unknown_connection_raises(self):
        with self.assertRaises(dbx_token_service.MissingServicePrincipal):
            dbx_token_service.get_dbx_token('cnx_nope')


class AuthRetryTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant('b.hub1', 'app_a')
        self.cid = compute_connection_id('b.hub1', 'p1', WS, 'c1')
        conns.insert(connection_id=self.cid, hub_id='b.hub1', project_id='p1',
                     dbx_workspace_url=WS, catalog='c1')
        get_secret_store().put_secret('ref/sp', 'sp-secret')
        conns.save_dbx_credentials(self.cid, workspace_url=WS, client_id='sp-id',
                                   client_secret_ref='ref/sp')
        dbx_token_service.invalidate()
        self.addCleanup(dbx_token_service.invalidate)
        self.mint_calls = []

        def fake_mint(workspace_url, client_id, client_secret, **kw):
            self.mint_calls.append(client_id)
            return m2m_auth_client.TokenResponse(
                access_token=f'tok-{len(self.mint_calls)}', expires_in=3600,
            )

        patcher = mock.patch.object(dbx_token_service, 'mint_workspace_token', fake_mint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_is_auth_error_detects_401_and_403_only(self):
        self.assertTrue(dbx_token_service.is_auth_error(RuntimeError('401 Unauthorized')))
        self.assertTrue(dbx_token_service.is_auth_error(RuntimeError('403 Forbidden')))
        self.assertFalse(dbx_token_service.is_auth_error(RuntimeError('500 Server Error')))
        self.assertFalse(dbx_token_service.is_auth_error(RuntimeError('404 Not Found')))
        self.assertFalse(dbx_token_service.is_auth_error(ValueError('bad input')))

    def test_success_runs_the_operation_once(self):
        seen = []
        result = dbx_token_service.with_auth_retry(
            self.cid, lambda token: seen.append(token) or 'done',
        )
        self.assertEqual(result, 'done')
        self.assertEqual(seen, ['tok-1'])
        self.assertEqual(len(self.mint_calls), 1)

    def test_401_forces_a_remint_and_retries_once(self):
        attempts = []

        def operation(token):
            attempts.append(token)
            if len(attempts) == 1:
                raise RuntimeError('401 Unauthorized')
            return 'recovered'

        self.assertEqual(dbx_token_service.with_auth_retry(self.cid, operation), 'recovered')
        self.assertEqual(attempts, ['tok-1', 'tok-2'])
        self.assertEqual(len(self.mint_calls), 2)

    def test_retries_at_most_once(self):
        attempts = []

        def always_401(token):
            attempts.append(token)
            raise RuntimeError('401 Unauthorized')

        with self.assertRaises(RuntimeError):
            dbx_token_service.with_auth_retry(self.cid, always_401)
        self.assertEqual(len(attempts), 2)

    def test_non_auth_errors_are_not_retried(self):
        attempts = []

        def boom(token):
            attempts.append(token)
            raise RuntimeError('500 Internal Server Error')

        with self.assertRaises(RuntimeError):
            dbx_token_service.with_auth_retry(self.cid, boom)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.mint_calls), 1)


if __name__ == '__main__':
    unittest.main()
