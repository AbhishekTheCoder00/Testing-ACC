"""
Purpose: sync_orchestrator._run_dc_export is the most complex untested function in the repo and
has just been extracted into a shared library the M2M path will drive. This is the safety net:
a recording harness captures every ACC call, Databricks call and run-state transition from the
pre-refactor path and from the shared path, and the tapes must match — for snapshot and CDC,
with the notebook-download path off and on, and on failure. Editing a test here to make it pass
means the refactor changed behaviour.
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

from backend.services.sync import sync_orchestrator as orch  # noqa: E402
from backend.services.sync import sync_steps  # noqa: E402

USER = 'alice'
WS = 'https://dbc-abc123.cloud.databricks.com'

ACC_CFG = {
    'project_id': 'b.proj-1',
    'hub_id': 'b.hub-1',
    'acc_account_id': 'acct-1',
    'project_name': 'Tower',
}
BS_STATE = {
    'snapshot_pipeline_id': 'pipe-snap',
    'cdc_pipeline_id': 'pipe-cdc',
    'snapshot_workflow_id': 111,
    'cdc_workflow_id': 222,
    'catalog_name': 'bronze_acme',
    'volume_path': '/Volumes/bronze_acme/bronze/acc_bronze_volume',
    'warehouse_id': 'wh-1',
}


class Tape:
    """One shared recording of everything both implementations do."""

    def __init__(self):
        self.events: list[tuple] = []

    def add(self, *event):
        self.events.append(event)


class FakeDbx:
    def __init__(self, tape: Tape):
        self.tape = tape

    def pipeline_has_active_update(self, pipeline_id):
        self.tape.add('pipeline_has_active_update', pipeline_id)
        return False

    def poll_pipeline_update(self, pipeline_id, update_id, max_wait_min=None,
                             before_poll=None):
        self.tape.add('poll_pipeline_update', pipeline_id, update_id, max_wait_min)
        return 'COMPLETED'

    def run_combined_workflow(self, workflow_id, **kwargs):
        self.tape.add('run_combined_workflow', workflow_id,
                      sorted(kwargs.get('pipeline_configs', {}).keys()),
                      kwargs.get('manifest_path'), kwargs.get('cdc_manifest_path'))
        return 9001

    def run_workflow(self, workflow_id, **kwargs):
        self.tape.add('run_workflow', workflow_id, kwargs.get('pipeline_id'),
                      kwargs.get('manifest_path'))
        return 9002

    def poll_workflow_run(self, run_id, max_wait_min=None, before_poll=None):
        self.tape.add('poll_workflow_run', run_id, max_wait_min)
        return 'SUCCESS'


class _SyncTestCase(unittest.TestCase):
    """Patches every collaborator of both implementations onto one tape."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {'ENABLE_NOTEBOOK_DOWNLOAD': 'false'})
        env.start()
        self.addCleanup(env.stop)

    def install(self, tape: Tape, *, notebook_download=False, fail_at=None):
        dbx = FakeDbx(tape)
        state = {'run_id': 0}

        def fake_db_create(user_id, project_id, data_types, trigger_type='manual'):
            state['run_id'] += 1
            tape.add('create_run', project_id, tuple(data_types), trigger_type)
            return state['run_id']

        def fake_db_update(run_id, st, **kw):
            tape.add('update_run', run_id, st, tuple(sorted(kw.keys())))

        def fake_dc_create(token, account_id, project_id, **kw):
            tape.add('dc_create_request', account_id, project_id,
                     kw.get('start_date'), tuple(kw.get('service_groups') or ()))
            return 'req-1'

        def fake_dc_create_cdc(token, account_id, project_id, **kw):
            tape.add('dc_create_cdc_request', account_id, project_id,
                     kw.get('start_date'), tuple(kw.get('service_groups') or ()))
            return 'req-cdc'

        def fake_wait(get_token, account_id, request_id):
            tape.add('dc_wait_for_job', account_id, request_id)
            return f'job-{request_id}'

        def fake_poll(get_token, account_id, job_id):
            tape.add('dc_poll_job', account_id, job_id)
            return {}

        def fake_flask(dbx_, get_token, account_id, job_id, vol_dir):
            tape.add('phase2_in_flask', account_id, job_id, vol_dir)
            if fail_at == 'download':
                raise RuntimeError('download exploded')
            return b'zip-bytes', 12

        def fake_notebook(dbx_, get_token, account_id, job_id, vol_dir):
            tape.add('phase2_via_notebook', account_id, job_id, vol_dir)
            return f'{vol_dir}/manifest.json', 12

        def fake_apply(dbx_, pipeline_id, conf, label='', full_refresh=False):
            tape.add('apply_and_start_pipeline_update', pipeline_id, label, full_refresh,
                     conf.get('acc.dc_snapshot_path'), conf.get('acc.dc_cdc_path'))
            if fail_at == 'pipeline':
                raise RuntimeError('pipeline exploded')
            return f'update-{pipeline_id}'

        def fake_window(user_id, project_id, key):
            tape.add('resolve_window', key)
            return ('2026-01-01T00:00:00.000Z', '2026-01-02T00:00:00.000Z')

        def fake_commit(user_id, project_id, *, mode, trigger_type, completion_ts=None):
            tape.add('commit_watermarks', mode, trigger_type)

        def fake_seed(dbx_, warehouse_id, catalog, doc):
            tape.add('seed_registry', warehouse_id, catalog)
            return 5, 1

        patches = [
            mock.patch.object(orch, 'DatabricksClient', lambda url, pat: dbx),
            mock.patch.object(orch, 'get_valid_dbx_token',
                              lambda uid: {'workspace_url': WS, 'access_token': 'pat'}),
            mock.patch.object(orch, 'apply_valid_dbx_token_to_client',
                              lambda uid, c: None),
            mock.patch.object(orch, 'resolve_incremental_window', fake_window),
            mock.patch.object(orch, 'commit_successful_watermarks_u2m', fake_commit),
            mock.patch.object(sync_steps, 'apply_and_start_pipeline_update', fake_apply),
            mock.patch.object(orch, 'apply_and_start_pipeline_update', fake_apply),
            mock.patch.object(sync_steps, '_phase2_in_flask', fake_flask),
            mock.patch.object(orch, '_phase2_in_flask', fake_flask),
            mock.patch.object(sync_steps, '_phase2_via_notebook', fake_notebook),
            mock.patch.object(orch, '_phase2_via_notebook', fake_notebook),
            mock.patch.object(sync_steps, '_load_schema_doc_from_zip', lambda z: {'d': 1}),
            mock.patch.object(orch, '_load_schema_doc_from_zip', lambda z: {'d': 1}),
            mock.patch.object(sync_steps, '_seed_registry_from_schema', fake_seed),
            mock.patch.object(orch, '_seed_registry_from_schema', fake_seed),
            mock.patch.object(sync_steps, '_ENABLE_NOTEBOOK_DOWNLOAD', notebook_download),
            mock.patch.object(orch, '_ENABLE_NOTEBOOK_DOWNLOAD', notebook_download),
        ]
        db_mock = mock.MagicMock()
        db_mock.get_acc_config.return_value = ACC_CFG
        db_mock.get_bootstrap_state.return_value = BS_STATE
        db_mock.is_daily_cdc_enabled.return_value = True
        db_mock.create_sync_run.side_effect = fake_db_create
        db_mock.update_sync_run.side_effect = fake_db_update
        db_mock.get_watermark.return_value = None
        patches.append(mock.patch.object(orch, 'db', db_mock))

        acc_mock = mock.MagicMock()
        acc_mock.dc_create_request.side_effect = fake_dc_create
        acc_mock.dc_create_cdc_request.side_effect = fake_dc_create_cdc
        acc_mock.dc_wait_for_job.side_effect = fake_wait
        acc_mock.dc_poll_job.side_effect = fake_poll
        acc_mock.get_valid_token.return_value = 'acc-tok'
        patches.append(mock.patch.object(orch, 'acc_client', acc_mock))
        patches.append(mock.patch.object(sync_steps, 'acc_client', acc_mock))

        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, shared: bool, mode: str, **install_kw) -> Tape:
        tape = Tape()
        self.install(tape, **install_kw)
        fn = orch._run_dc_export_shared if shared else orch._run_dc_export_legacy
        try:
            fn(USER, 'manual', mode)
        except RuntimeError as exc:
            tape.add('RAISED', str(exc))
        return tape


class EquivalenceTests(_SyncTestCase):
    def assert_same(self, mode, **kw):
        legacy = self.capture(False, mode, **kw)
        shared = self.capture(True, mode, **kw)
        self.assertEqual(legacy.events, shared.events)
        return shared

    def test_snapshot_pipeline_path_is_identical(self):
        tape = self.assert_same('snapshot')
        names = [e[0] for e in tape.events]
        self.assertIn('dc_create_request', names)
        self.assertIn('dc_create_cdc_request', names)   # cdc baseline
        self.assertIn('commit_watermarks', names)

    def test_cdc_pipeline_path_is_identical(self):
        tape = self.assert_same('cdc')
        names = [e[0] for e in tape.events]
        self.assertIn('dc_create_cdc_request', names)
        self.assertNotIn('dc_create_request', names)

    def test_snapshot_workflow_path_is_identical(self):
        tape = self.assert_same('snapshot', notebook_download=True)
        names = [e[0] for e in tape.events]
        self.assertIn('run_combined_workflow', names)
        self.assertIn('phase2_via_notebook', names)

    def test_cdc_workflow_path_is_identical(self):
        tape = self.assert_same('cdc', notebook_download=True)
        self.assertIn('run_workflow', [e[0] for e in tape.events])

    def test_download_failure_is_identical(self):
        tape = self.assert_same('cdc', fail_at='download')
        self.assertEqual(tape.events[-1][0], 'RAISED')
        self.assertIn('download exploded', tape.events[-1][1])

    def test_pipeline_failure_is_identical(self):
        tape = self.assert_same('cdc', fail_at='pipeline')
        self.assertEqual(tape.events[-1][0], 'RAISED')

    def test_watermarks_are_not_committed_on_failure(self):
        """FR-03 §18 Q7 — a failed run must not advance the anchor."""
        tape = self.assert_same('cdc', fail_at='pipeline')
        self.assertNotIn('commit_watermarks', [e[0] for e in tape.events])

    def test_run_state_transitions_are_identical(self):
        legacy = self.capture(False, 'snapshot')
        shared = self.capture(True, 'snapshot')
        states = lambda t: [e[2] for e in t.events if e[0] == 'update_run']
        self.assertEqual(states(legacy), states(shared))
        self.assertEqual(states(shared)[-1], 'complete')

    def test_failure_state_is_recorded_in_both(self):
        legacy = self.capture(False, 'cdc', fail_at='pipeline')
        shared = self.capture(True, 'cdc', fail_at='pipeline')
        for tape in (legacy, shared):
            self.assertIn('failed', [e[2] for e in tape.events if e[0] == 'update_run'])


class PortsTests(_SyncTestCase):
    def test_u2m_port_drops_the_phase_field(self):
        """`phase` exists only on the connection-scoped table; U2M must not receive it."""
        captured = []
        db_mock = mock.MagicMock()
        db_mock.update_sync_run.side_effect = lambda rid, st, **kw: captured.append(kw)
        with mock.patch.object(orch, 'db', db_mock):
            ports = orch._u2m_ports(USER, ACC_CFG, mock.MagicMock())
            ports.update_run(1, 'failed', error='boom', phase='dc_export')
        self.assertEqual(captured, [{'error': 'boom'}])

    def test_shared_steps_reject_an_unknown_mode(self):
        with self.assertRaises(ValueError):
            sync_steps.run_export(
                mock.MagicMock(), sync_steps.SyncTarget('a', 'p', 'p', 'c', '/v'),
                mock.MagicMock(), mode='sideways', trigger_type='manual',
            )

    def test_assert_pipelines_idle_blocks_an_active_update(self):
        dbx = mock.MagicMock()
        dbx.pipeline_has_active_update.return_value = True
        target = sync_steps.SyncTarget('a', 'p', 'p', 'c', '/v',
                                       snapshot_pipeline_id='pipe-snap')
        with self.assertRaises(RuntimeError) as ctx:
            sync_steps.assert_pipelines_idle(dbx, target)
        self.assertIn('pipe-snap', str(ctx.exception))

    def test_assert_pipelines_idle_skips_unprovisioned_pipelines(self):
        dbx = mock.MagicMock()
        dbx.pipeline_has_active_update.return_value = True
        sync_steps.assert_pipelines_idle(
            dbx, sync_steps.SyncTarget('a', 'p', 'p', 'c', '/v'),
        )


class DispatchTests(unittest.TestCase):
    def test_dispatch_follows_the_flag(self):
        with mock.patch.object(orch, '_run_dc_export_shared') as shared, \
             mock.patch.object(orch, '_run_dc_export_legacy') as legacy:
            with mock.patch.dict(os.environ, {'USE_SHARED_STEPS': 'true'}):
                orch._run_dc_export(USER, 'manual', 'snapshot')
            shared.assert_called_once()
            legacy.assert_not_called()

            shared.reset_mock()
            with mock.patch.dict(os.environ, {'USE_SHARED_STEPS': 'false'}):
                orch._run_dc_export(USER, 'manual', 'snapshot')
            legacy.assert_called_once()

    def test_run_sync_and_run_cdc_sync_route_through_the_dispatcher(self):
        with mock.patch.object(orch, '_run_dc_export') as dispatch:
            orch.run_sync(USER)
            orch.run_cdc_sync(USER)
        self.assertEqual(
            [c.kwargs['mode'] for c in dispatch.call_args_list], ['snapshot', 'cdc'],
        )


if __name__ == '__main__':
    unittest.main()
