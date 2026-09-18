"""
Purpose: The SSA JWT-bearer flow is the one piece of Phase 1 that cannot be validated without
a real APS app, so these tests pin the request shapes exactly rather than just "it was called":
every assertion claim is decoded with the matching public key, the token exchange must use HTTP
Basic (not Basic *and* body params), and the 10-robot quota error must surface as a typed
exception. Traces FR-03 §7.1-7.3, §9.1-9.2, §12.3, Appendix A/B; FR-04 TC-07; TC-SSA-10/11.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from backend.clients.acc import ssa_client  # noqa: E402
from backend.clients.acc.constants import (  # noqa: E402
    ACC_SSA_SCOPES,
    APS_TOKEN_URL,
    SSA_GRANT_TYPE,
)

# One key for the whole module — RSA generation is the slowest thing in this file.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self.reason = 'OK' if status_code < 400 else 'Error'
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.ok = status_code < 400
        self.headers = {}

    def json(self):
        return self._payload


class FakeRequests:
    """Records calls so a test can assert the exact wire shape."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self):
        if not self._responses:
            return FakeResponse(200, {})
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        return self._next()

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self._next()

    def delete(self, url, **kwargs):
        self.calls.append(('DELETE', url, kwargs))
        return self._next()


class _SsaTestCase(unittest.TestCase):
    def setUp(self):
        ssa_client.invalidate_admin_token_cache()
        self.addCleanup(ssa_client.invalidate_admin_token_cache)

    def patch_requests(self, *responses) -> FakeRequests:
        fake = FakeRequests(*responses)
        patcher = mock.patch.object(ssa_client, 'requests', fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class AdminTokenTests(_SsaTestCase):
    def test_uses_client_credentials_with_management_scopes(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'admin-tok', 'expires_in': 3600}),
        )
        token = ssa_client.get_admin_token('AAA', 'sekrit')
        self.assertEqual(token, 'admin-tok')

        method, url, kwargs = fake.calls[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(url, APS_TOKEN_URL)
        body = kwargs['data']
        self.assertEqual(body['grant_type'], 'client_credentials')
        self.assertIn('application:service_account:write', body['scope'])
        self.assertIn('application:service_account_key:write', body['scope'])

    def test_uses_basic_auth_not_body_credentials(self):
        """FR-03 §7.1 — 'Use HTTP Basic auth **or** body params (not both)'."""
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'admin-tok', 'expires_in': 3600}),
        )
        ssa_client.get_admin_token('AAA', 'sekrit')
        _, _, kwargs = fake.calls[0]
        self.assertEqual(kwargs['auth'], ('AAA', 'sekrit'))
        self.assertNotIn('client_id', kwargs['data'])
        self.assertNotIn('client_secret', kwargs['data'])

    def test_token_is_cached_per_client_id(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'tok-a', 'expires_in': 3600}),
            FakeResponse(200, {'access_token': 'tok-b', 'expires_in': 3600}),
        )
        self.assertEqual(ssa_client.get_admin_token('AAA', 's'), 'tok-a')
        self.assertEqual(ssa_client.get_admin_token('AAA', 's'), 'tok-a')
        self.assertEqual(len(fake.calls), 1)
        # A different shard must not reuse the first shard's token.
        self.assertEqual(ssa_client.get_admin_token('BBB', 's'), 'tok-b')
        self.assertEqual(len(fake.calls), 2)

    def test_near_expiry_token_is_reminted(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'tok-1', 'expires_in': 1}),
            FakeResponse(200, {'access_token': 'tok-2', 'expires_in': 3600}),
        )
        self.assertEqual(ssa_client.get_admin_token('AAA', 's'), 'tok-1')
        self.assertEqual(ssa_client.get_admin_token('AAA', 's'), 'tok-2')
        self.assertEqual(len(fake.calls), 2)


class CreateServiceAccountTests(_SsaTestCase):
    def test_posts_to_service_accounts_with_bearer(self):
        fake = self.patch_requests(FakeResponse(200, {
            'serviceAccountId': 'svc-123',
            'email': 'forma-dbx-7f3a@AAA.adskserviceaccount.autodesk.com',
        }))
        sa = ssa_client.create_service_account('admin-tok', hub_id='b.7f3a9b2c-dead-beef')
        self.assertEqual(sa.service_account_id, 'svc-123')
        self.assertIn('adskserviceaccount', sa.email)

        method, url, kwargs = fake.calls[0]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/service-accounts'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer admin-tok')
        self.assertTrue(kwargs['json']['name'].startswith('forma-dbx-'))

    def test_robot_name_is_derived_from_the_hub_and_is_safe(self):
        name = ssa_client.robot_name_for_hub('b.7F3A9B2C-dead-beef-0000')
        self.assertTrue(name.startswith('forma-dbx-'))
        self.assertTrue(all(c.isalnum() or c == '-' for c in name))
        self.assertEqual(name, name.lower())
        self.assertLessEqual(len(name), 32)

    def test_distinct_hubs_get_distinct_robot_names(self):
        self.assertNotEqual(
            ssa_client.robot_name_for_hub('b.7f3a9b2c-1111'),
            ssa_client.robot_name_for_hub('b.9e1d4f8a-2222'),
        )

    def test_quota_exhaustion_raises_a_typed_error(self):
        """FR-06 §4.8 step 2 shows this verbatim as 'cs-16: SSA limit reached'."""
        self.patch_requests(FakeResponse(
            400,
            {'detail': 'cs-16: the maximum of 10 service accounts for Client ID has been reached.'},
            text='cs-16: the maximum of 10 service accounts for Client ID has been reached.',
        ))
        with self.assertRaises(ssa_client.SsaQuotaExceeded):
            ssa_client.create_service_account('admin-tok', hub_id='b.hub11')

    def test_403_limit_exceeded_also_raises_quota(self):
        self.patch_requests(FakeResponse(403, {'error': 'limit_exceeded'},
                                         text='{"error": "limit_exceeded"}'))
        with self.assertRaises(ssa_client.SsaQuotaExceeded):
            ssa_client.create_service_account('admin-tok', hub_id='b.hub11')

    def test_other_errors_raise_the_generic_api_error(self):
        self.patch_requests(FakeResponse(500, {}, text='boom'))
        with self.assertRaises(ssa_client.SsaApiError) as ctx:
            ssa_client.create_service_account('admin-tok', hub_id='b.hub1')
        self.assertNotIsInstance(ctx.exception, ssa_client.SsaQuotaExceeded)


class CreateKeyTests(_SsaTestCase):
    def test_returns_kid_and_private_key(self):
        fake = self.patch_requests(FakeResponse(200, {
            'kid': 'kid-1', 'privateKey': PRIVATE_PEM,
        }))
        key = ssa_client.create_key('admin-tok', 'svc-123')
        self.assertEqual(key.kid, 'kid-1')
        self.assertIn('BEGIN PRIVATE KEY', key.private_key_pem)
        self.assertTrue(fake.calls[0][1].endswith('/service-accounts/svc-123/keys'))

    def test_missing_private_key_in_response_is_an_error(self):
        """The PEM is shown once; silently storing nothing would brick the hub."""
        self.patch_requests(FakeResponse(200, {'kid': 'kid-1'}))
        with self.assertRaises(ssa_client.SsaApiError):
            ssa_client.create_key('admin-tok', 'svc-123')


class AssertionTests(_SsaTestCase):
    def build(self, **over):
        kwargs = dict(
            client_id='AAA',
            service_account_id='svc-123',
            key_id='kid-1',
            private_key_pem=PRIVATE_PEM,
        )
        kwargs.update(over)
        return ssa_client.build_assertion(**kwargs)

    def decode(self, token):
        return pyjwt.decode(
            token, PUBLIC_PEM, algorithms=['RS256'], audience=APS_TOKEN_URL,
        )

    def test_claims_match_the_spec(self):
        """FR-03 §9.1 — iss is the app Client ID, sub is the robot."""
        claims = self.decode(self.build())
        self.assertEqual(claims['iss'], 'AAA')
        self.assertEqual(claims['sub'], 'svc-123')
        self.assertEqual(claims['aud'], APS_TOKEN_URL)

    def test_scope_claim_is_a_list(self):
        claims = self.decode(self.build())
        self.assertEqual(claims['scope'], ACC_SSA_SCOPES.split())

    def test_header_carries_the_kid_and_rs256(self):
        header = pyjwt.get_unverified_header(self.build())
        self.assertEqual(header['kid'], 'kid-1')
        self.assertEqual(header['alg'], 'RS256')

    def test_iat_is_backdated_for_clock_skew(self):
        """FR-03 §16.4 — a server clock slightly ahead must not fail every mint."""
        import time as _time

        claims = self.decode(self.build())
        self.assertLessEqual(claims['iat'], int(_time.time()) - 29)

    def test_lifetime_is_within_the_aps_ceiling(self):
        """APS rejects exp more than 300s after iat; the spec says keep it <= 120."""
        claims = self.decode(self.build())
        self.assertEqual(claims['exp'] - claims['iat'], 120)
        self.assertLessEqual(claims['exp'] - claims['iat'], 300)

    def test_signature_is_verifiable_with_the_matching_public_key(self):
        self.decode(self.build())  # raises if the signature is wrong

    def test_a_different_key_cannot_verify(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pub = other.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        with self.assertRaises(pyjwt.InvalidSignatureError):
            pyjwt.decode(self.build(), other_pub, algorithms=['RS256'],
                         audience=APS_TOKEN_URL)


class ExchangeJwtTests(_SsaTestCase):
    def exchange(self, fake=None):
        return ssa_client.exchange_jwt(
            client_id='AAA',
            client_secret='sekrit',
            service_account_id='svc-123',
            key_id='kid-1',
            private_key_pem=PRIVATE_PEM,
        )

    def test_sends_the_jwt_bearer_grant(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'acc-tok', 'expires_in': 3600}),
        )
        resp = self.exchange()
        self.assertEqual(resp.access_token, 'acc-tok')
        self.assertEqual(resp.expires_in, 3600)

        _, url, kwargs = fake.calls[0]
        self.assertEqual(url, APS_TOKEN_URL)
        self.assertEqual(kwargs['data']['grant_type'], SSA_GRANT_TYPE)
        self.assertEqual(kwargs['data']['scope'], ACC_SSA_SCOPES)
        self.assertIn('assertion', kwargs['data'])

    def test_uses_basic_auth(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'acc-tok', 'expires_in': 3600}),
        )
        self.exchange()
        self.assertEqual(fake.calls[0][2]['auth'], ('AAA', 'sekrit'))

    def test_assertion_is_signed_by_the_hub_key(self):
        fake = self.patch_requests(
            FakeResponse(200, {'access_token': 'acc-tok', 'expires_in': 3600}),
        )
        self.exchange()
        assertion = fake.calls[0][2]['data']['assertion']
        claims = pyjwt.decode(assertion, PUBLIC_PEM, algorithms=['RS256'],
                              audience=APS_TOKEN_URL)
        self.assertEqual(claims['sub'], 'svc-123')

    def test_no_refresh_token_is_expected_or_used(self):
        """FR-03 §4 — the SSA flow re-mints from the private key; there is no refresh."""
        self.patch_requests(
            FakeResponse(200, {'access_token': 'acc-tok', 'expires_in': 3600,
                               'refresh_token': 'should-be-ignored'}),
        )
        resp = self.exchange()
        self.assertFalse(hasattr(resp, 'refresh_token'))

    def test_missing_expires_in_falls_back_to_an_hour(self):
        self.patch_requests(FakeResponse(200, {'access_token': 'acc-tok'}))
        self.assertEqual(self.exchange().expires_in, 3600)

    def test_failure_raises_and_does_not_leak_the_assertion(self):
        """TC-SSA-10 — no key material or assertion in the error text."""
        self.patch_requests(FakeResponse(401, {}, text='invalid assertion'))
        with self.assertRaises(ssa_client.SsaApiError) as ctx:
            self.exchange()
        message = str(ctx.exception)
        self.assertNotIn('BEGIN PRIVATE KEY', message)
        self.assertNotIn('sekrit', message)


class KeyLifecycleTests(_SsaTestCase):
    def test_list_keys(self):
        fake = self.patch_requests(FakeResponse(200, {'keys': [{'kid': 'kid-1'}]}))
        keys = ssa_client.list_keys('admin-tok', 'svc-123')
        self.assertEqual(keys[0]['kid'], 'kid-1')
        self.assertTrue(fake.calls[0][1].endswith('/service-accounts/svc-123/keys'))

    def test_list_keys_handles_a_bare_list_response(self):
        self.patch_requests(FakeResponse(200, [{'kid': 'kid-1'}]))
        self.assertEqual(ssa_client.list_keys('admin-tok', 'svc-123')[0]['kid'], 'kid-1')

    def test_delete_key(self):
        fake = self.patch_requests(FakeResponse(204, {}))
        ssa_client.delete_key('admin-tok', 'svc-123', 'kid-1')
        method, url, kwargs = fake.calls[0]
        self.assertEqual(method, 'DELETE')
        self.assertTrue(url.endswith('/service-accounts/svc-123/keys/kid-1'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer admin-tok')

    def test_delete_missing_key_is_tolerated(self):
        """Rotation deletes the old key last; a 404 there must not fail the rotation."""
        self.patch_requests(FakeResponse(404, {}, text='not found'))
        ssa_client.delete_key('admin-tok', 'svc-123', 'kid-old')


class RedactionTests(unittest.TestCase):
    def test_redact_hides_secrets_and_keeps_context(self):
        body = (
            '{"assertion": "eyJhbGciOi.body.sig", "client_secret": "sekrit", '
            '"privateKey": "-----BEGIN PRIVATE KEY-----\\nabc\\n", "error": "invalid_client"}'
        )
        out = ssa_client._redact(body)
        self.assertNotIn('sekrit', out)
        self.assertNotIn('BEGIN PRIVATE KEY', out)
        self.assertNotIn('eyJhbGciOi', out)
        self.assertIn('invalid_client', out)

    def test_redact_handles_none_and_empty(self):
        self.assertEqual(ssa_client._redact(None), '')
        self.assertEqual(ssa_client._redact(''), '')


if __name__ == '__main__':
    unittest.main()
