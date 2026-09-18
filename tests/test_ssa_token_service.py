"""
Purpose: Scheduled sync must reach ACC as the hub's robot and never as a human (FR-02 FR-04,
FR-25, BR-08) — the outage this whole model exists to prevent is a human's refresh token dying.
So the load-bearing assertions here are that no `acc_tokens` row is ever read, that the token is
minted from the hub's own key under the app recorded on its tenant, and that the returned getter
matches the shape `data_connector_client` already retries with. Traces TC-SYNC-02/04, TC-SSA-11.
"""

from __future__ import annotations

import sys
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
from backend.services.m2m import ssa_token_service as svc  # noqa: E402

HUB = 'b.hub-aaa'
PEM = '-----BEGIN PRIVATE KEY-----\nhubA\n-----END PRIVATE KEY-----\n'


class _TokenTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/aps-app-a-secret', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-a-secret', 'app-secret')
        self.seed_hub(HUB, 'svc-1', PEM)
        svc.invalidate()
        self.addCleanup(svc.invalidate)

    def seed_hub(self, hub_id, service_account_id, pem, app_ref='app_a'):
        tenants.ensure_tenant(hub_id, app_ref)
        key_ref = f'ref/hub/{hub_id}/ssa-private-key'
        get_secret_store().put_secret(key_ref, pem)
        ssa.insert(
            hub_id=hub_id, aps_app_ref=app_ref, service_account_id=service_account_id,
            robot_email=f'{service_account_id}@AAA.adskserviceaccount.autodesk.com',
            key_id=f'kid-{service_account_id}', private_key_ref=key_ref,
        )

    def patch_exchange(self, *tokens):
        """tokens: (access_token, expires_in) pairs; the last repeats."""
        calls = []

        def fake_exchange(**kwargs):
            calls.append(kwargs)
            token, expires = tokens[min(len(calls) - 1, len(tokens) - 1)]
            return ssa_client.TokenResponse(access_token=token, expires_in=expires)

        patcher = mock.patch.object(ssa_client, 'exchange_jwt', fake_exchange)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class MintingTests(_TokenTestCase):
    def test_mints_with_this_hubs_key_and_app(self):
        calls = self.patch_exchange(('acc-tok', 3600))
        self.assertEqual(svc.get_acc_token(HUB), 'acc-tok')

        call = calls[0]
        self.assertEqual(call['client_id'], 'AAA')
        self.assertEqual(call['client_secret'], 'app-secret')
        self.assertEqual(call['service_account_id'], 'svc-1')
        self.assertEqual(call['private_key_pem'], PEM)
        self.assertEqual(call['key_id'], 'kid-svc-1')

    def test_never_reads_a_human_token_row(self):
        """TC-SYNC-04 / BR-08 — the whole point of the SSA model."""
        self.patch_exchange(('acc-tok', 3600))
        with mock.patch(
            'backend.repositories.state.token_repository.get_acc_tokens',
        ) as get_human:
            svc.get_acc_token(HUB)
        get_human.assert_not_called()

    def test_two_hubs_mint_independently(self):
        other_pem = '-----BEGIN PRIVATE KEY-----\nhubB\n-----END PRIVATE KEY-----\n'
        self.seed_hub('b.hub-bbb', 'svc-2', other_pem)
        calls = self.patch_exchange(('tok-a', 3600), ('tok-b', 3600))
        self.assertEqual(svc.get_acc_token(HUB), 'tok-a')
        self.assertEqual(svc.get_acc_token('b.hub-bbb'), 'tok-b')
        self.assertEqual(calls[1]['service_account_id'], 'svc-2')
        self.assertEqual(calls[1]['private_key_pem'], other_pem)

    def test_hub_without_a_robot_raises_clearly(self):
        self.patch_exchange(('t', 3600))
        with self.assertRaises(svc.MissingServiceAccount):
            svc.get_acc_token('b.hub-none')

    def test_app_is_resolved_from_the_tenant_not_the_default(self):
        """FR-04 TC-04 — a robot's key only works under the Client ID it was created with."""
        apps.insert('app_b', 'BBB', 'ref/aps-app-b-secret', max_robots=10)
        get_secret_store().put_secret('ref/aps-app-b-secret', 'b-secret')
        self.seed_hub('b.hub-on-b', 'svc-9', PEM, app_ref='app_b')
        calls = self.patch_exchange(('t', 3600))
        svc.get_acc_token('b.hub-on-b')
        self.assertEqual(calls[0]['client_id'], 'BBB')
        self.assertEqual(calls[0]['client_secret'], 'b-secret')


class CachingTests(_TokenTestCase):
    def test_cache_hit_does_not_re_sign(self):
        calls = self.patch_exchange(('tok-1', 3600), ('tok-2', 3600))
        self.assertEqual(svc.get_acc_token(HUB), 'tok-1')
        self.assertEqual(svc.get_acc_token(HUB), 'tok-1')
        self.assertEqual(len(calls), 1)

    def test_near_expiry_is_reminted_before_it_dies(self):
        """A DC export can run for minutes; a token expiring mid-poll is a real failure."""
        calls = self.patch_exchange(('tok-1', 30), ('tok-2', 3600))
        self.assertEqual(svc.get_acc_token(HUB), 'tok-1')
        self.assertEqual(svc.get_acc_token(HUB), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_force_bypasses_the_cache(self):
        calls = self.patch_exchange(('tok-1', 3600), ('tok-2', 3600))
        svc.get_acc_token(HUB)
        self.assertEqual(svc.get_acc_token(HUB, force=True), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_invalidate_one_hub_leaves_others_cached(self):
        self.seed_hub('b.hub-bbb', 'svc-2', PEM)
        calls = self.patch_exchange(('t1', 3600), ('t2', 3600), ('t3', 3600))
        svc.get_acc_token(HUB)
        svc.get_acc_token('b.hub-bbb')
        svc.invalidate(HUB)
        svc.get_acc_token(HUB)
        svc.get_acc_token('b.hub-bbb')
        self.assertEqual(len(calls), 3)

    def test_invalidate_all(self):
        calls = self.patch_exchange(('t1', 3600), ('t2', 3600))
        svc.get_acc_token(HUB)
        svc.invalidate()
        svc.get_acc_token(HUB)
        self.assertEqual(len(calls), 2)

    def test_buffer_matches_the_documented_value(self):
        self.assertEqual(svc.TOKEN_MINT_BUFFER_SEC, 120)


class TokenGetterContractTests(_TokenTestCase):
    def test_getter_is_callable_with_no_arguments(self):
        """`data_connector_client._dc_get` calls `get_token()` on the happy path."""
        self.patch_exchange(('acc-tok', 3600))
        self.assertEqual(svc.token_getter(HUB)(), 'acc-tok')

    def test_getter_refresh_true_forces_a_remint(self):
        """...and `get_token(refresh=True)` after a 401, which must bypass the cache."""
        calls = self.patch_exchange(('tok-1', 3600), ('tok-2', 3600))
        getter = svc.token_getter(HUB)
        self.assertEqual(getter(), 'tok-1')
        self.assertEqual(getter(refresh=True), 'tok-2')
        self.assertEqual(len(calls), 2)

    def test_getter_is_bound_to_one_hub(self):
        self.seed_hub('b.hub-bbb', 'svc-2', PEM)
        calls = self.patch_exchange(('t', 3600))
        svc.token_getter('b.hub-bbb')()
        self.assertEqual(calls[0]['service_account_id'], 'svc-2')

    def test_works_as_the_data_connector_token_getter(self):
        """Smoke-check the real integration point: _dc_get retries with this shape."""
        from backend.clients.acc import data_connector_client as dc

        self.patch_exchange(('acc-tok', 3600))
        seen = []
        with mock.patch.object(dc, '_get', lambda url, token, **kw: seen.append(token) or {}):
            dc._dc_get(svc.token_getter(HUB), 'https://example.test/x')
        self.assertEqual(seen, ['acc-tok'])


if __name__ == '__main__':
    unittest.main()
