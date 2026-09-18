"""
Purpose: A connection is one installed pipeline and owns all of its operational state —
pipeline ids, bootstrap, watermarks and run history (FR-02 FR-23). These tests pin that the
state really is per-connection and shared across a hub's admins, that reads are tenant-scoped,
and that the in-flight guard uses exactly the FR-05 §10.3 state set. Traces TC-CONN-02/03/04/
05/07/10/12/13, TC-CONC-03, TC-SYNC-05/06/10.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _dbharness import TempDbTestCase  # noqa: E402

from backend.repositories.state import (  # noqa: E402
    aps_app_repository as apps,
    connection_repository as conns,
    connection_sync_repository as runs,
    tenant_repository as tenants,
)
from backend.services.m2m.connection_identity import compute_connection_id  # noqa: E402

HUB = 'b.hub1'
WS = 'https://dbc-abc123.cloud.databricks.com'


class _ConnectionTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        tenants.ensure_tenant('b.hub2', 'app_a')

    def make(self, project='p1', catalog='c1', hub=HUB, workspace=WS, name=None):
        cid = compute_connection_id(hub, project, workspace, catalog)
        return conns.insert(
            connection_id=cid,
            hub_id=hub,
            project_id=project,
            project_name=name or f'Project {project}',
            dbx_workspace_url=workspace,
            catalog=catalog,
        )


class ConnectionRepositoryTests(_ConnectionTestCase):
    def test_insert_and_get(self):
        row = self.make()
        self.assertEqual(row['hub_id'], HUB)
        self.assertEqual(row['onboarding_status'], 'pending_custom_integration')
        self.assertFalse(row['cdc_enabled'])
        self.assertEqual(conns.get(row['connection_id'])['catalog'], 'c1')

    def test_get_missing_returns_none(self):
        self.assertIsNone(conns.get('cnx_nope'))

    def test_get_by_parts_round_trips(self):
        """TC-CONN-02 — reselecting the same target must find the existing row."""
        made = self.make()
        found = conns.get_by_parts(HUB, 'p1', WS, 'c1')
        self.assertEqual(found['connection_id'], made['connection_id'])

    def test_get_by_parts_ignores_a_trailing_slash(self):
        made = self.make()
        self.assertEqual(
            conns.get_by_parts(HUB, 'p1', WS + '/', 'c1')['connection_id'],
            made['connection_id'],
        )

    def test_new_project_is_a_new_connection(self):
        """TC-CONN-03."""
        a = self.make(project='p1')
        b = self.make(project='p2')
        self.assertNotEqual(a['connection_id'], b['connection_id'])

    def test_new_catalog_is_a_new_connection(self):
        """TC-CONN-04."""
        a = self.make(catalog='c1')
        b = self.make(catalog='c2')
        self.assertNotEqual(a['connection_id'], b['connection_id'])

    def test_list_for_hub_is_shared_across_admins_and_scoped(self):
        """TC-CONN-05 plus tenant isolation — hub 2 never sees hub 1's connections."""
        self.make(project='p1')
        self.make(project='p2')
        self.make(hub='b.hub2', project='p9')
        self.assertEqual(len(conns.list_for_hub(HUB)), 2)
        self.assertEqual(len(conns.list_for_hub('b.hub2')), 1)

    def test_update_status_and_error(self):
        cid = self.make()['connection_id']
        conns.update_status(cid, 'pending_bootstrap', last_error='boom')
        row = conns.get(cid)
        self.assertEqual(row['onboarding_status'], 'pending_bootstrap')
        self.assertEqual(row['last_error'], 'boom')
        conns.update_status(cid, 'ready')
        self.assertIsNone(conns.get(cid)['last_error'])

    def test_unknown_status_is_rejected(self):
        cid = self.make()['connection_id']
        with self.assertRaises(ValueError):
            conns.update_status(cid, 'almost_ready')

    def test_set_pipeline_ids(self):
        """TC-CONN-07 — each connection owns its own pipelines."""
        a = self.make(project='p1')['connection_id']
        b = self.make(project='p2')['connection_id']
        conns.set_pipeline_ids(a, snapshot_pipeline_id='snapA', cdc_pipeline_id='cdcA')
        conns.set_pipeline_ids(b, snapshot_pipeline_id='snapB', cdc_pipeline_id='cdcB')
        self.assertEqual(conns.get(a)['snapshot_pipeline_id'], 'snapA')
        self.assertEqual(conns.get(b)['cdc_pipeline_id'], 'cdcB')

    def test_adding_a_connection_leaves_others_alone(self):
        """TC-CONN-12."""
        a = self.make(project='p1')['connection_id']
        conns.set_pipeline_ids(a, snapshot_pipeline_id='snapA', cdc_pipeline_id='cdcA')
        conns.update_status(a, 'ready')
        self.make(project='p2')
        row = conns.get(a)
        self.assertEqual(row['onboarding_status'], 'ready')
        self.assertEqual(row['snapshot_pipeline_id'], 'snapA')


class DbxCredentialTests(_ConnectionTestCase):
    def test_save_and_get(self):
        cid = self.make()['connection_id']
        self.assertFalse(conns.has_dbx_credentials(cid))
        conns.save_dbx_credentials(
            cid, workspace_url=WS, client_id='sp-1', client_secret_ref='ref/sp',
        )
        self.assertTrue(conns.has_dbx_credentials(cid))
        row = conns.get_dbx_credentials(cid)
        self.assertEqual(row['client_id'], 'sp-1')
        self.assertEqual(row['client_secret_ref'], 'ref/sp')

    def test_never_stores_the_secret_itself(self):
        cid = self.make()['connection_id']
        conns.save_dbx_credentials(
            cid, workspace_url=WS, client_id='sp-1', client_secret_ref='ref/sp',
        )
        self.assertNotIn('client_secret', conns.get_dbx_credentials(cid))

    def test_save_is_an_upsert(self):
        cid = self.make()['connection_id']
        conns.save_dbx_credentials(cid, workspace_url=WS, client_id='a', client_secret_ref='r1')
        conns.save_dbx_credentials(cid, workspace_url=WS, client_id='b', client_secret_ref='r2')
        self.assertEqual(conns.get_dbx_credentials(cid)['client_id'], 'b')


class ScheduleTests(_ConnectionTestCase):
    def _ready(self, project='p1'):
        cid = self.make(project=project)['connection_id']
        conns.update_status(cid, 'ready')
        conns.save_dbx_credentials(
            cid, workspace_url=WS, client_id='sp', client_secret_ref='ref',
        )
        return cid

    def test_set_schedule_toggles_cdc(self):
        cid = self._ready()
        conns.set_schedule(cid, enabled=True, next_run_at=1000.0)
        row = conns.get(cid)
        self.assertTrue(row['cdc_enabled'])
        self.assertEqual(row['auto_cdc_next_run_at'], 1000.0)
        conns.set_schedule(cid, enabled=False)
        self.assertFalse(conns.get(cid)['cdc_enabled'])

    def test_list_due_requires_ready_enabled_and_credentials(self):
        """The scheduler must skip anything that cannot actually run headlessly (D-4)."""
        due = self._ready('p1')
        conns.set_schedule(due, enabled=True, next_run_at=100.0)

        not_ready = self.make(project='p2')['connection_id']
        conns.set_schedule(not_ready, enabled=True, next_run_at=100.0)

        no_sp = self.make(project='p3')['connection_id']
        conns.update_status(no_sp, 'ready')
        conns.set_schedule(no_sp, enabled=True, next_run_at=100.0)

        disabled = self._ready('p4')

        future = self._ready('p5')
        conns.set_schedule(future, enabled=True, next_run_at=10_000.0)

        ids = [c['connection_id'] for c in conns.list_due_for_cdc(now=500.0)]
        self.assertEqual(ids, [due])
        self.assertNotIn(disabled, ids)
        self.assertNotIn(future, ids)

    def test_null_next_run_at_is_due(self):
        """A connection whose schedule was just enabled should run on the next tick."""
        cid = self._ready()
        conns.set_schedule(cid, enabled=True, next_run_at=None)
        ids = [c['connection_id'] for c in conns.list_due_for_cdc(now=1.0)]
        self.assertEqual(ids, [cid])


class BootstrapStateTests(_ConnectionTestCase):
    def test_save_and_get_is_per_connection(self):
        """TC-CONN-13 — bootstrap state hangs off the connection, not a user."""
        a = self.make(project='p1')['connection_id']
        b = self.make(project='p2')['connection_id']
        conns.save_bootstrap(a, catalog_name='c1', volume_path='/Volumes/c1/bronze/v',
                             warehouse_id='wh-1', notebook_folder='/Users/x/acc/v1')
        conns.save_bootstrap(b, catalog_name='c2', volume_path='/Volumes/c2/bronze/v')
        self.assertEqual(conns.get_bootstrap(a)['catalog_name'], 'c1')
        self.assertEqual(conns.get_bootstrap(b)['catalog_name'], 'c2')
        self.assertIsNotNone(conns.get_bootstrap(a)['bootstrapped_at'])

    def test_save_is_an_upsert(self):
        cid = self.make()['connection_id']
        conns.save_bootstrap(cid, catalog_name='c1', warehouse_id='wh-1')
        conns.save_bootstrap(cid, catalog_name='c1', warehouse_id='wh-2')
        self.assertEqual(conns.get_bootstrap(cid)['warehouse_id'], 'wh-2')

    def test_carries_workflow_and_job_ids(self):
        cid = self.make()['connection_id']
        conns.save_bootstrap(
            cid, catalog_name='c1', snapshot_workflow_id=11, cdc_workflow_id=22,
            download_job_id=33, catalog_claim_id=44,
        )
        row = conns.get_bootstrap(cid)
        self.assertEqual(row['snapshot_workflow_id'], 11)
        self.assertEqual(row['cdc_workflow_id'], 22)
        self.assertEqual(row['download_job_id'], 33)
        self.assertEqual(row['catalog_claim_id'], 44)

    def test_get_missing_returns_none(self):
        self.assertIsNone(conns.get_bootstrap(self.make()['connection_id']))


class SyncRunTests(_ConnectionTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make()['connection_id']

    def test_create_and_read_back(self):
        run_id = runs.create_run(self.cid, mode='snapshot', trigger_type='manual')
        row = runs.get_run(run_id)
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['mode'], 'snapshot')
        self.assertEqual(row['trigger_type'], 'manual')
        self.assertEqual(row['connection_id'], self.cid)

    def test_update_records_phase_and_error(self):
        """FR-02 FR-27 / TC-SYNC-05 — a failure names the run and the phase."""
        run_id = runs.create_run(self.cid, mode='cdc')
        runs.update_run(run_id, 'failed', phase='dc_export', error='403 from DC')
        row = runs.get_run(run_id)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['phase'], 'dc_export')
        self.assertEqual(row['error'], '403 from DC')

    def test_unknown_state_is_rejected(self):
        run_id = runs.create_run(self.cid, mode='cdc')
        with self.assertRaises(ValueError):
            runs.update_run(run_id, 'nearly_done')

    def test_in_flight_states_match_the_spec(self):
        """FR-05 §10.3 exactly."""
        self.assertEqual(
            set(runs.IN_FLIGHT_STATES),
            {'pending', 'exporting', 'downloading', 'pipeline_running'},
        )

    def test_has_in_flight_run_for_each_active_state(self):
        for state in runs.IN_FLIGHT_STATES:
            run_id = runs.create_run(self.cid, mode='snapshot')
            runs.update_run(run_id, state)
            self.assertIsNotNone(runs.has_in_flight_run(self.cid), state)
            runs.update_run(run_id, 'cancelled')

    def test_terminal_states_are_not_in_flight(self):
        for state in ('success', 'failed', 'cancelled'):
            run_id = runs.create_run(self.cid, mode='snapshot')
            runs.update_run(run_id, state)
            self.assertIsNone(runs.has_in_flight_run(self.cid), state)

    def test_in_flight_is_per_connection_not_per_hub(self):
        """FR-03 §18 Q4/Q7 — two connections on one hub may run in parallel."""
        other = self.make(project='p2')['connection_id']
        runs.update_run(runs.create_run(self.cid, mode='snapshot'), 'exporting')
        self.assertIsNotNone(runs.has_in_flight_run(self.cid))
        self.assertIsNone(runs.has_in_flight_run(other))

    def test_latest_and_list_are_scoped_to_the_connection(self):
        other = self.make(project='p2')['connection_id']
        runs.create_run(self.cid, mode='snapshot')
        mine = runs.create_run(self.cid, mode='cdc')
        runs.create_run(other, mode='snapshot')
        self.assertEqual(runs.latest_run(self.cid)['run_id'], mine)
        self.assertEqual(len(runs.list_runs(self.cid)), 2)
        self.assertEqual(len(runs.list_runs(other)), 1)

    def test_list_runs_is_newest_first_and_limited(self):
        for _ in range(5):
            runs.create_run(self.cid, mode='cdc')
        rows = runs.list_runs(self.cid, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertGreater(rows[0]['run_id'], rows[-1]['run_id'])


class WatermarkTests(_ConnectionTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make()['connection_id']

    def test_set_and_get(self):
        self.assertIsNone(runs.get_watermark(self.cid))
        runs.set_watermark(self.cid, 1234.0)
        self.assertEqual(runs.get_watermark(self.cid), 1234.0)

    def test_upsert(self):
        runs.set_watermark(self.cid, 1.0)
        runs.set_watermark(self.cid, 2.0)
        self.assertEqual(runs.get_watermark(self.cid), 2.0)

    def test_types_are_independent(self):
        runs.set_watermark(self.cid, 1.0, data_type='data_connector')
        runs.set_watermark(self.cid, 2.0, data_type='data_connector_cdc')
        self.assertEqual(runs.get_watermark(self.cid, 'data_connector'), 1.0)
        self.assertEqual(runs.get_watermark(self.cid, 'data_connector_cdc'), 2.0)

    def test_watermarks_are_per_connection(self):
        other = self.make(project='p2')['connection_id']
        runs.set_watermark(self.cid, 1.0)
        self.assertIsNone(runs.get_watermark(other))

    def test_last_successful_only_counts_successful_runs(self):
        """TC-SYNC-06 — a failed retry must not advance the anchor."""
        failed = runs.create_run(self.cid, mode='snapshot')
        runs.update_run(failed, 'failed', error='boom')
        self.assertIsNone(runs.last_successful_timestamp(self.cid, 'data_connector'))

        ok = runs.create_run(self.cid, mode='snapshot')
        runs.update_run(ok, 'success')
        self.assertIsNotNone(runs.last_successful_timestamp(self.cid, 'data_connector'))

    def test_cdc_run_does_not_advance_the_snapshot_anchor(self):
        """A cdc run advances the cdc watermark only — snapshot keeps its own anchor."""
        cdc = runs.create_run(self.cid, mode='cdc')
        runs.update_run(cdc, 'success')
        self.assertIsNone(runs.last_successful_timestamp(self.cid, 'data_connector'))
        self.assertIsNotNone(runs.last_successful_timestamp(self.cid, 'data_connector_cdc'))

    def test_snapshot_run_advances_both_anchors(self):
        snap = runs.create_run(self.cid, mode='snapshot')
        runs.update_run(snap, 'success')
        self.assertIsNotNone(runs.last_successful_timestamp(self.cid, 'data_connector'))
        self.assertIsNotNone(runs.last_successful_timestamp(self.cid, 'data_connector_cdc'))


class DeleteTests(_ConnectionTestCase):
    """TC-CONN-14 — SQLite does not honour ON DELETE CASCADE unless the foreign_keys pragma
    is on, so the repository deletes child rows explicitly (FR-05 §9.3). If that ever
    regressed, a deleted connection would leave watermarks and run rows that a re-created
    connection with the same deterministic id would silently inherit."""

    def setUp(self):
        super().setUp()
        self.cid = self.make()['connection_id']
        conns.save_dbx_credentials(
            self.cid, workspace_url=WS, client_id='sp-1', client_secret_ref='ref/sp-1',
        )
        conns.save_bootstrap(self.cid, catalog_name='c1', warehouse_id='wh-1')
        self.run_id = runs.create_run(self.cid, mode='snapshot')
        runs.set_watermark(self.cid, 123.0, 'data_connector')

    def test_delete_removes_every_child_row(self):
        conns.delete(self.cid)
        self.assertIsNone(conns.get(self.cid))
        self.assertIsNone(conns.get_bootstrap(self.cid))
        self.assertIsNone(conns.get_dbx_credentials(self.cid))
        self.assertIsNone(runs.get_run(self.run_id))
        self.assertIsNone(runs.get_watermark(self.cid, 'data_connector'))

    def test_delete_leaves_another_connection_alone(self):
        other = self.make(project='p2', catalog='c2')['connection_id']
        runs.set_watermark(other, 456.0, 'data_connector')
        conns.delete(self.cid)
        self.assertIsNotNone(conns.get(other))
        self.assertEqual(runs.get_watermark(other, 'data_connector'), 456.0)

    def test_recreating_after_a_delete_starts_clean(self):
        conns.delete(self.cid)
        recreated = self.make()['connection_id']
        self.assertEqual(recreated, self.cid)
        self.assertEqual(runs.list_runs(recreated), [])
        self.assertIsNone(runs.get_watermark(recreated, 'data_connector'))


if __name__ == '__main__':
    unittest.main()
