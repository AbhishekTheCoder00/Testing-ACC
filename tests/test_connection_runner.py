"""
Purpose: The connection runner is the headless half of the product, so these tests pin the
guarantees that make it headless: the ACC token comes from the hub's service account and no
acc_tokens row is ever read (BR-08), Databricks auth comes from the connection's service
principal, run rows and watermarks land in the connection-keyed tables, and watermarks move
only on success so a failed retry cannot shrink the next window.
Traces TC-SYNC-02…10, TC-CONC-03, FR-03 §11.3/11.4/§18 Q7.
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
    connection_sync_repository as runs,
    tenant_repository as tenants,
)
from backend.services.m2m import connection_runner as runner  # noqa: E402
from backend.services.m2m import connection_service as svc  # noqa: E402
from backend.services.sync.sync_config import (  # noqa: E402
    DC_CDC_WATERMARK_KEY,
    DC_WATERMARK_KEY,
)

HUB = 'b.hub1'
WS = 'https://dbc-abc123.cloud.databricks.com'


class _RunnerTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        self.connection = svc.create_or_resume(
            hub_id=HUB, project_id='b.proj1', dbx_workspace_url=WS, catalog='c1',
        ).connection
        self.cid = self.connection['connection_id']
        conns.save_dbx_credentials(
            self.cid, workspace_url=WS, client_id='sp-1',
            client_secret_ref='ref/sp-1', validated_at=1.0,
        )
        conns.set_pipeline_ids(self.cid, snapshot_pipeline_id='snap-1',
                               cdc_pipeline_id='cdc-1')
        conns.save_bootstrap(
            self.cid, catalog_name='c1',
            volume_path='/Volumes/c1/bronze/acc_bronze_volume',
            warehouse_id='wh-1', snapshot_workflow_id=111, cdc_workflow_id=222,
            notebook_folder='/Shared/acc',
        )
        conns.update_status(self.cid, 'ready')

        self.clients = []
        patches = [
            mock.patch.object(runner, 'DatabricksClient', side_effect=self._make_client),
            mock.patch.object(runner.dbx_token_service, 'get_dbx_token',
                              return_value='sp-token'),
            mock.patch.object(runner.ssa_token_service, 'token_getter',
                              side_effect=self._token_getter),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_client(self, workspace_url, token):
        """Stands in for DatabricksClient, honouring update_token so a mid-flight re-mint
        is observable the same way the real client makes it observable."""
        client = mock.Mock(name='dbx')
        client.workspace_url = workspace_url
        client.token = token
        client.update_token.side_effect = (
            lambda new_token: setattr(client, 'token', new_token)
        )
        self.clients.append(client)
        return client

    def _token_getter(self, hub_id):
        self.token_getter_hub = hub_id
        return lambda refresh=False: f'ssa-token-for-{hub_id}'

    def run_sync(self, mode='snapshot', trigger_type='manual', side_effect=None,
                 result=None, **kwargs):
        default = {'mode': mode, 'run_id': 1, 'state': 'complete', 'file_count': 3}
        with mock.patch.object(
            runner.sync_steps, 'run_export',
            return_value=result or default, side_effect=side_effect,
        ) as run_export:
            self.run_export = run_export
            return runner.run_connection_sync(
                self.cid, mode=mode, trigger_type=trigger_type, **kwargs,
            )

    def ports(self):
        return self.run_export.call_args.args[2]

    def target(self):
        return self.run_export.call_args.args[1]


class TargetTests(_RunnerTestCase):
    def test_the_target_is_built_from_the_connection_and_its_bootstrap(self):
        self.run_sync()
        target = self.target()
        self.assertEqual(target.catalog_name, 'c1')
        self.assertEqual(target.volume_base, '/Volumes/c1/bronze/acc_bronze_volume')
        self.assertEqual(target.snapshot_pipeline_id, 'snap-1')
        self.assertEqual(target.cdc_pipeline_id, 'cdc-1')
        self.assertEqual(target.snapshot_workflow_id, 111)
        self.assertEqual(target.cdc_workflow_id, 222)
        self.assertEqual(target.warehouse_id, 'wh-1')

    def test_the_account_id_is_derived_from_the_hub_id(self):
        """TC-SYNC-09 — acc_account_id is the de-prefixed hub, used only in the DC URL."""
        self.run_sync()
        self.assertEqual(self.target().account_id, 'hub1')

    def test_the_project_id_is_carried_both_prefixed_and_bare(self):
        self.run_sync()
        self.assertEqual(self.target().project_id, 'b.proj1')
        self.assertEqual(self.target().bare_project_id, 'proj1')


class CredentialTests(_RunnerTestCase):
    def test_the_acc_token_comes_from_the_hubs_service_account(self):
        """TC-SYNC-02 — headless ACC auth is the hub SSA, never a human's token."""
        self.run_sync()
        self.assertEqual(self.token_getter_hub, HUB)
        self.assertEqual(self.ports().get_acc_token(), f'ssa-token-for-{HUB}')

    def test_no_acc_tokens_row_is_ever_read(self):
        """TC-SYNC-04 — the BR-08 guarantee, asserted at the client boundary."""
        from backend.clients import acc_client

        with mock.patch.object(acc_client, 'get_valid_token',
                               side_effect=AssertionError('read a human token')):
            self.run_sync()
            self.ports().get_acc_token()

    def test_databricks_auth_is_the_connections_service_principal(self):
        """TC-SYNC-03 — client_credentials, no refresh-token grant on this path."""
        self.run_sync()
        self.assertEqual(self.clients[0].token, 'sp-token')
        self.assertEqual(self.clients[0].workspace_url, WS)

    def test_refresh_dbx_remints_and_reapplies_the_token(self):
        self.run_sync()
        with mock.patch.object(runner.dbx_token_service, 'get_dbx_token',
                               return_value='sp-token-2') as mint:
            self.ports().refresh_dbx()
        mint.assert_called_once_with(self.cid, force=True)
        self.assertEqual(self.clients[0].token, 'sp-token-2')

    def test_a_connection_without_a_service_principal_can_still_be_synced_by_hand(self):
        """D-4 — manual sync may use the wizard's Databricks token."""
        conns.delete(self.cid)
        connection = svc.create_or_resume(
            hub_id=HUB, project_id='b.proj1', dbx_workspace_url=WS, catalog='c1',
        ).connection
        conns.set_pipeline_ids(connection['connection_id'],
                               snapshot_pipeline_id='snap-1', cdc_pipeline_id='cdc-1')
        conns.save_bootstrap(connection['connection_id'], catalog_name='c1',
                             volume_path='/Volumes/c1/bronze/acc_bronze_volume')
        conns.update_status(connection['connection_id'], 'ready')
        with mock.patch.object(runner.dbx_token_service, 'get_dbx_token',
                               side_effect=AssertionError('must not mint')):
            self.run_sync(user_token='u2m-token')
        self.assertEqual(self.clients[0].token, 'u2m-token')

    def test_a_scheduled_run_refuses_to_borrow_a_human_token(self):
        """BR-08 — a cron tick must never fall back to a user's Databricks token."""
        conns.save_dbx_credentials  # noqa: B018 - documents the guard below
        with mock.patch.object(conns, 'has_dbx_credentials', return_value=False):
            with self.assertRaises(runner.SyncNotAuthorised):
                self.run_sync(trigger_type='scheduled', user_token='u2m-token')


class PortsTests(_RunnerTestCase):
    def test_create_run_writes_to_the_connection_keyed_table(self):
        self.run_sync()
        run_id = self.ports().create_run('snapshot', 'manual', DC_WATERMARK_KEY)
        row = runs.get_run(run_id)
        self.assertEqual(row['connection_id'], self.cid)
        self.assertEqual(row['mode'], 'snapshot')
        self.assertEqual(row['trigger_type'], 'manual')
        self.assertEqual(row['state'], 'pending')

    def test_the_u2m_state_vocabulary_is_translated(self):
        """The shared steps speak one vocabulary; this table stores the FR-05 §10.3 one."""
        self.run_sync()
        ports = self.ports()
        run_id = ports.create_run('snapshot', 'manual', DC_WATERMARK_KEY)
        for shared_state, stored in (
            ('running', 'pending'),
            ('dc_job_submitted', 'exporting'),
            ('uploading_files', 'downloading'),
            ('bronze_written', 'downloading'),
            ('workflow_triggered', 'pipeline_running'),
            ('bronze_job_running', 'pipeline_running'),
            ('complete', 'success'),
        ):
            ports.update_run(run_id, shared_state)
            self.assertEqual(runs.get_run(run_id)['state'], stored, shared_state)

    def test_an_unknown_shared_state_is_rejected_rather_than_stored_wrong(self):
        self.run_sync()
        ports = self.ports()
        run_id = ports.create_run('cdc', 'scheduled', DC_CDC_WATERMARK_KEY)
        with self.assertRaises(ValueError):
            ports.update_run(run_id, 'teleporting')

    def test_a_failure_records_the_phase_and_error(self):
        """TC-SYNC-05 — a failed run must say which connection, run and phase."""
        self.run_sync()
        ports = self.ports()
        run_id = ports.create_run('cdc', 'scheduled', DC_CDC_WATERMARK_KEY)
        ports.update_run(run_id, 'failed', error='DC export refused', phase='dc_export')
        row = runs.get_run(run_id)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['phase'], 'dc_export')
        self.assertEqual(row['error'], 'DC export refused')
        self.assertEqual(row['connection_id'], self.cid)

    def test_file_counts_survive_the_translation(self):
        self.run_sync()
        ports = self.ports()
        run_id = ports.create_run('cdc', 'manual', DC_CDC_WATERMARK_KEY)
        ports.update_run(run_id, 'bronze_written', record_counts='{"files": 7}')
        self.assertEqual(runs.get_run(run_id)['record_counts'], '{"files": 7}')

    def test_a_snapshot_commits_both_watermarks(self):
        self.run_sync()
        self.ports().commit_watermarks('snapshot', 'manual')
        self.assertIsNotNone(runs.get_watermark(self.cid, DC_WATERMARK_KEY))
        self.assertIsNotNone(runs.get_watermark(self.cid, DC_CDC_WATERMARK_KEY))

    def test_a_cdc_run_commits_only_the_cdc_watermark(self):
        self.run_sync(mode='cdc')
        self.ports().commit_watermarks('cdc', 'scheduled')
        self.assertIsNone(runs.get_watermark(self.cid, DC_WATERMARK_KEY))
        self.assertIsNotNone(runs.get_watermark(self.cid, DC_CDC_WATERMARK_KEY))

    def test_the_window_is_a_full_export_until_something_succeeds(self):
        self.run_sync()
        self.assertEqual(self.ports().resolve_window(DC_CDC_WATERMARK_KEY), (None, None))

    def test_the_window_anchors_on_the_last_successful_run(self):
        """TC-SYNC-06 — a failed retry must not shrink the next window."""
        self.run_sync()
        ports = self.ports()
        good = ports.create_run('cdc', 'scheduled', DC_CDC_WATERMARK_KEY)
        ports.update_run(good, 'complete')
        start, end = ports.resolve_window(DC_CDC_WATERMARK_KEY)
        self.assertIsNotNone(start)

        bad = ports.create_run('cdc', 'scheduled', DC_CDC_WATERMARK_KEY)
        ports.update_run(bad, 'failed', error='boom', phase='dc_export')
        self.assertEqual(ports.resolve_window(DC_CDC_WATERMARK_KEY)[0], start)


class GuardTests(_RunnerTestCase):
    def test_an_in_flight_run_blocks_a_second_start(self):
        """FR-03 §12.2 — one connection, one run at a time."""
        runs.create_run(self.cid, mode='snapshot', trigger_type='manual')
        with mock.patch.object(runner.sync_steps, 'run_export',
                               side_effect=AssertionError('must not sync')):
            with self.assertRaises(runner.SyncInFlight):
                runner.run_connection_sync(self.cid, mode='snapshot')

    def test_two_connections_on_one_hub_run_in_parallel(self):
        """TC-CONC-03 — the guard is per connection, not per hub."""
        other = svc.create_or_resume(
            hub_id=HUB, project_id='b.proj2', dbx_workspace_url=WS, catalog='c2',
        ).connection
        runs.create_run(self.cid, mode='snapshot', trigger_type='manual')
        self.assertIsNone(runs.has_in_flight_run(other['connection_id']))

    def test_a_terminal_run_does_not_block(self):
        run_id = runs.create_run(self.cid, mode='snapshot', trigger_type='manual')
        runs.update_run(run_id, 'success')
        self.run_sync()
        self.assertEqual(self.run_export.call_count, 1)

    def test_an_unbootstrapped_connection_is_refused(self):
        conns.update_status(self.cid, 'pending_bootstrap')
        with mock.patch.object(runner.sync_steps, 'run_export',
                               side_effect=AssertionError('must not sync')):
            with self.assertRaises(runner.SyncNotReady):
                runner.run_connection_sync(self.cid, mode='snapshot')

    def test_a_missing_snapshot_pipeline_is_refused_with_a_rerun_bootstrap_message(self):
        conns.set_pipeline_ids(self.cid, snapshot_pipeline_id=None, cdc_pipeline_id='cdc-1')
        with self.assertRaises(runner.SyncNotReady) as caught:
            self.run_sync()
        self.assertIn('bootstrap', str(caught.exception).lower())

    def test_a_missing_cdc_pipeline_is_refused_for_a_cdc_run(self):
        conns.set_pipeline_ids(self.cid, snapshot_pipeline_id='snap-1', cdc_pipeline_id=None)
        with self.assertRaises(runner.SyncNotReady):
            self.run_sync(mode='cdc')

    def test_an_unknown_connection_is_refused(self):
        with self.assertRaises(runner.UnknownConnection):
            runner.run_connection_sync('cnx_nope', mode='snapshot')

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_sync(mode='sideways')

    def test_a_bootstrap_row_is_required(self):
        from backend.repositories.state.database import _conn

        with _conn() as con:
            con.execute('DELETE FROM connection_bootstrap WHERE connection_id = ?',
                        (self.cid,))
        with self.assertRaises(runner.SyncNotReady):
            self.run_sync()


class DelegationTests(_RunnerTestCase):
    def test_the_sequence_is_delegated_to_the_shared_steps(self):
        """D-1 — the runner must not re-implement the export sequence."""
        out = self.run_sync(mode='cdc', trigger_type='scheduled')
        self.assertEqual(self.run_export.call_count, 1)
        self.assertEqual(self.run_export.call_args.kwargs,
                         {'mode': 'cdc', 'trigger_type': 'scheduled'})
        self.assertEqual(out['state'], 'complete')

    def test_a_failure_propagates_to_the_caller(self):
        with self.assertRaises(RuntimeError):
            self.run_sync(side_effect=RuntimeError('DC refused'))

    def test_a_permissions_failure_reverifies_the_robot(self):
        """D-17 — a robot removed from the project must not stay green in our DB."""
        with mock.patch.object(runner.provisioning_probe,
                               'verify_robot_on_project') as probe:
            with self.assertRaises(RuntimeError):
                self.run_sync(side_effect=RuntimeError('403 Forbidden from Data Connector'))
        probe.assert_called_once_with(HUB, 'b.proj1')

    def test_an_ordinary_failure_does_not_reverify(self):
        with mock.patch.object(runner.provisioning_probe, 'verify_robot_on_project',
                               side_effect=AssertionError('must not probe')):
            with self.assertRaises(RuntimeError):
                self.run_sync(side_effect=RuntimeError('warehouse did not start'))


if __name__ == '__main__':
    unittest.main()
