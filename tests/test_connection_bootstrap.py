"""
Purpose: The M2M bootstrap driver must be a driver and nothing more — the provisioning
sequence itself belongs to the shared step library (D-1/D-2). These tests pin the four things
the driver does own: which Databricks token it uses (service principal first, wizard token as
the D-4 fallback), connection-keyed progress, persistence into connection_bootstrap, and that
a mid-sequence failure leaves the connection retryable rather than half-ready.
Traces FR-03 §11.2, FR-06 §4.8 step 4.
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
    connection_repository as conns,
    tenant_repository as tenants,
)
from backend.services.m2m import connection_bootstrap as cb  # noqa: E402
from backend.services.m2m import connection_service as svc  # noqa: E402

HUB = 'b.hub1'
WS = 'https://dbc-abc123.cloud.databricks.com'

_RESULT = {
    'snapshot_pipeline_id': 'snap-1',
    'cdc_pipeline_id': 'cdc-1',
    'snapshot_workflow_id': 111,
    'cdc_workflow_id': 222,
    'notebook_folder': '/Shared/acc',
    'volume_path': '/Volumes/c1/bronze/acc_bronze_volume',
    'compute_type': 'sql_warehouse',
    'warehouse_id': 'wh-1',
    'catalog_name': 'c1',
    'pipeline_names': {'snapshot_pipeline': 'snap', 'cdc_pipeline': 'cdc'},
}


class _BootstrapTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        self.connection = svc.create_or_resume(
            hub_id=HUB, project_id='p1', dbx_workspace_url=WS, catalog='c1',
            project_name='Riverside',
        ).connection
        self.cid = self.connection['connection_id']
        conns.update_status(self.cid, 'pending_bootstrap')
        cb.reset_progress(self.cid)
        self.addCleanup(cb.reset_progress, self.cid)

        self.clients = []
        client_patch = mock.patch.object(
            cb, 'DatabricksClient', side_effect=self._make_client,
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def _make_client(self, workspace_url, token):
        client = mock.Mock(name=f'dbx({token})')
        client.workspace_url = workspace_url
        client.token = token
        self.clients.append(client)
        return client

    def add_sp(self):
        conns.save_dbx_credentials(
            self.cid, workspace_url=WS, client_id='sp-1',
            client_secret_ref='ref/sp-1', validated_at=1.0,
        )

    def run_bootstrap(self, *, result=None, side_effect=None, **kwargs):
        with mock.patch.object(
            cb.bootstrap_steps, 'run_all',
            return_value=result or dict(_RESULT), side_effect=side_effect,
        ) as run_all:
            self.run_all = run_all
            return cb.run_connection_bootstrap(self.cid, **kwargs)


class TokenChoiceTests(_BootstrapTestCase):
    def test_uses_the_service_principal_token_when_one_exists(self):
        self.add_sp()
        with mock.patch.object(cb.dbx_token_service, 'get_dbx_token',
                               return_value='sp-token') as mint:
            self.run_bootstrap()
        mint.assert_called_once_with(self.cid)
        self.assertEqual(self.clients[0].token, 'sp-token')

    def test_falls_back_to_the_wizard_token_when_there_is_no_service_principal(self):
        """D-4 — a human's Databricks OAuth token may complete the wizard."""
        with mock.patch.object(cb.dbx_token_service, 'get_dbx_token',
                               side_effect=AssertionError('must not mint')):
            self.run_bootstrap(user_token='u2m-token')
        self.assertEqual(self.clients[0].token, 'u2m-token')

    def test_prefers_the_service_principal_over_a_supplied_wizard_token(self):
        self.add_sp()
        with mock.patch.object(cb.dbx_token_service, 'get_dbx_token',
                               return_value='sp-token'):
            self.run_bootstrap(user_token='u2m-token')
        self.assertEqual(self.clients[0].token, 'sp-token')

    def test_no_token_at_all_fails_before_calling_databricks(self):
        with mock.patch.object(cb.bootstrap_steps, 'run_all',
                               side_effect=AssertionError('must not provision')):
            with self.assertRaises(cb.BootstrapNotAuthorised):
                cb.run_connection_bootstrap(self.cid)
        self.assertEqual(self.clients, [])

    def test_the_workspace_url_comes_from_the_connection(self):
        self.run_bootstrap(user_token='u2m-token')
        self.assertEqual(self.clients[0].workspace_url, WS)


class DriverBehaviourTests(_BootstrapTestCase):
    def test_delegates_the_sequence_to_the_shared_steps(self):
        """D-1/D-2 — the driver must not re-implement any provisioning step."""
        self.run_bootstrap(user_token='t')
        self.assertEqual(self.run_all.call_count, 1)
        args, kwargs = self.run_all.call_args
        self.assertEqual(args[1], 'c1')
        self.assertTrue(callable(kwargs['progress']))

    def test_always_creates_the_sync_workflows(self):
        """§3.2.7 — flipping ENABLE_NOTEBOOK_DOWNLOAD must not strand the fleet."""
        self.run_bootstrap(user_token='t')
        self.assertIs(self.run_all.call_args.kwargs['create_workflows'], True)

    def test_persists_every_artefact_and_marks_the_connection_ready(self):
        tenants.update_status(HUB, 'whitelist_verified')
        out = self.run_bootstrap(user_token='t')
        saved = conns.get_bootstrap(self.cid)
        self.assertEqual(saved['snapshot_workflow_id'], 111)
        self.assertEqual(saved['cdc_workflow_id'], 222)
        self.assertEqual(saved['notebook_folder'], '/Shared/acc')
        self.assertEqual(saved['warehouse_id'], 'wh-1')
        self.assertEqual(saved['catalog_name'], 'c1')
        self.assertIsNotNone(saved['catalog_claim_id'])
        row = conns.get(self.cid)
        self.assertEqual(row['onboarding_status'], 'ready')
        self.assertEqual(row['snapshot_pipeline_id'], 'snap-1')
        self.assertEqual(row['cdc_pipeline_id'], 'cdc-1')
        self.assertEqual(tenants.get(HUB)['onboarding_status'], 'ssa_active')
        self.assertEqual(out['catalog_claim_id'], saved['catalog_claim_id'])

    def test_progress_is_keyed_by_connection_and_ends_done(self):
        self.run_bootstrap(user_token='t')
        progress = cb.get_progress(self.cid)
        self.assertTrue(progress['done'])
        self.assertIsNone(progress['error'])
        self.assertIn('complete', progress['message'].lower())

    def test_progress_of_an_unknown_connection_is_an_empty_shape(self):
        self.assertEqual(cb.get_progress('cnx_nope')['step'], 0)
        self.assertFalse(cb.get_progress('cnx_nope')['done'])

    def test_rerunning_is_idempotent(self):
        self.run_bootstrap(user_token='t')
        first = conns.get_bootstrap(self.cid)
        self.run_bootstrap(user_token='t')
        second = conns.get_bootstrap(self.cid)
        self.assertEqual(first['catalog_claim_id'], second['catalog_claim_id'])
        self.assertEqual(len(conns.list_for_hub(HUB)), 1)


class FailureTests(_BootstrapTestCase):
    def test_a_mid_sequence_failure_leaves_the_connection_retryable(self):
        with self.assertRaises(RuntimeError):
            self.run_bootstrap(user_token='t', side_effect=RuntimeError('catalog missing'))
        row = conns.get(self.cid)
        self.assertEqual(row['onboarding_status'], 'pending_bootstrap')
        self.assertIn('catalog missing', row['last_error'])
        self.assertIsNone(conns.get_bootstrap(self.cid))

    def test_a_failure_is_reported_through_progress(self):
        with self.assertRaises(RuntimeError):
            self.run_bootstrap(user_token='t', side_effect=RuntimeError('boom'))
        progress = cb.get_progress(self.cid)
        self.assertTrue(progress['done'])
        self.assertIn('boom', progress['error'])

    def test_a_second_bootstrap_while_one_is_running_is_refused(self):
        """The wizard polls; a double-click must not start two provisioning runs."""
        self.assertTrue(cb.try_begin(self.cid))
        self.addCleanup(cb.end, self.cid)
        self.assertFalse(cb.try_begin(self.cid))
        with mock.patch.object(cb.bootstrap_steps, 'run_all',
                               side_effect=AssertionError('must not provision')):
            with self.assertRaises(cb.BootstrapInFlight):
                cb.run_connection_bootstrap(self.cid, user_token='t')

    def test_in_flight_is_cleared_after_a_failure(self):
        with self.assertRaises(RuntimeError):
            self.run_bootstrap(user_token='t', side_effect=RuntimeError('boom'))
        self.assertTrue(cb.try_begin(self.cid))
        cb.end(self.cid)

    def test_an_unknown_connection_is_rejected(self):
        with self.assertRaises(cb.UnknownConnection):
            cb.run_connection_bootstrap('cnx_nope', user_token='t')

    def test_a_stolen_catalog_stops_the_bootstrap(self):
        """BR-11 — never provision into a catalog another connection now owns."""
        from backend.repositories.state import catalog_claim_repository as claim_repo

        claim = claim_repo.get_active_claim(claim_repo.normalize_workspace_key(WS), 'c1')
        claim_repo.release_claim(claim['claim_id'])
        claim_repo.claim_catalog(claim_repo.normalize_workspace_key(WS), 'c1', 'someone-else')
        with mock.patch.object(cb.bootstrap_steps, 'run_all',
                               side_effect=AssertionError('must not provision')):
            with self.assertRaises(svc.CatalogAlreadyConnected):
                cb.run_connection_bootstrap(self.cid, user_token='t')


if __name__ == '__main__':
    unittest.main()
