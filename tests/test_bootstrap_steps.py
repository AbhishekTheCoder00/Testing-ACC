"""
Purpose: bootstrap_service had no test coverage before Phase 1, and its 11-step sequence was
just extracted into a shared library that the M2M path will also drive. This is the safety net
for that refactor: a recording fake DatabricksClient captures every call and every progress
message from the pre-refactor path and from the shared path, and the two tapes must match
exactly. If a test here needs editing to pass, the refactor changed behaviour and is wrong.
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

from backend.services import bootstrap_service  # noqa: E402
from backend.services.provisioning import bootstrap_steps  # noqa: E402

CATALOG = 'bronze_acme_west'
CLAIM = {'claim_id': 42, 'owner_user_id': 'alice', 'catalog_name': CATALOG,
         'workspace_key': 'dbc-abc123'}


class RecordingDbx:
    """Records every Databricks call so two implementations can be compared."""

    def __init__(self, *, warehouses=None, volume_ok=True):
        self.calls: list[tuple] = []
        self._warehouses = warehouses if warehouses is not None else [
            {'id': 'wh-1', 'name': 'Serverless Starter', 'state': 'RUNNING'},
        ]
        self._volume_ok = volume_ok

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, tuple(sorted(kwargs.items()))))

    def get(self, path):
        self._record('get', path)
        if path == '/api/2.0/sql/warehouses':
            return {'warehouses': self._warehouses}
        return {}

    def wait_for_warehouse(self, warehouse_id):
        self._record('wait_for_warehouse', warehouse_id)

    def execute_sql(self, warehouse_id, sql):
        self._record('execute_sql', warehouse_id, ' '.join(sql.split()))
        return {'result': {'data_array': [['metastore-1']]}}

    def uc_verify_catalog_exists(self, catalog):
        self._record('uc_verify_catalog_exists', catalog)

    def uc_create_schema(self, catalog, schema):
        self._record('uc_create_schema', catalog, schema)

    def uc_create_volume(self, catalog, schema, volume):
        self._record('uc_create_volume', catalog, schema, volume)

    def validate_volume_path(self, path):
        self._record('validate_volume_path', path)
        return self._volume_ok

    def put_file(self, path, content):
        self._record('put_file', path, len(content))

    def get_current_user_email(self):
        self._record('get_current_user_email')
        return 'alice@acme.com'

    def mkdirs(self, path):
        self._record('mkdirs', path)

    def import_notebook(self, path, content_b64):
        self._record('import_notebook', path, len(content_b64))

    def get_or_create_pipeline(self, name, config):
        self._record('get_or_create_pipeline', name, config['catalog'],
                     config['edition'], config['libraries'][0]['notebook']['path'])
        return f'pipe-{name}'

    def create_combined_snapshot_workflow(self, name, snap_id, cdc_id, **kwargs):
        self._record('create_combined_snapshot_workflow', name, snap_id, cdc_id, **kwargs)
        return 111, {}

    def create_sync_workflow(self, name, nb_path, pipeline_id, **kwargs):
        self._record('create_sync_workflow', name, nb_path, pipeline_id, **kwargs)
        return 222, {}


class _BootstrapTestCase(unittest.TestCase):
    def setUp(self):
        self.progress: list[tuple] = []
        self.saved: dict = {}

        def record_progress(step, message, *, error=None):
            self.progress.append((step, message, error))

        self.record_progress = record_progress

        # Persistence is the driver's job and differs per path; stub it so the comparison
        # is about the Databricks work and the progress tape.
        self.db = mock.MagicMock()
        self.db.get_acc_config.return_value = None
        patcher = mock.patch.object(bootstrap_service, 'db', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict(os.environ, {'ENABLE_NOTEBOOK_DOWNLOAD': 'false'})
        env.start()
        self.addCleanup(env.stop)

    def run_legacy(self, dbx):
        captured = []
        with mock.patch.object(
            bootstrap_service, '_set_progress',
            lambda uid, step, message, done=False, error=None: captured.append(
                (step, message, error)),
        ):
            result = bootstrap_service._provision_catalog_legacy(
                'alice', 'https://dbc-abc123.cloud.databricks.com', 'pat', CATALOG, CLAIM,
            )
        return result, captured

    def run_shared(self, dbx):
        captured = []
        with mock.patch.object(
            bootstrap_service, '_set_progress',
            lambda uid, step, message, done=False, error=None: captured.append(
                (step, message, error)),
        ):
            result = bootstrap_service._provision_catalog_shared(
                'alice', 'https://dbc-abc123.cloud.databricks.com', 'pat', CATALOG, CLAIM,
            )
        return result, captured


class EquivalenceTests(_BootstrapTestCase):
    """The refactor is only safe if these pass without being edited."""

    def _both(self, **dbx_kwargs):
        legacy_dbx = RecordingDbx(**dbx_kwargs)
        shared_dbx = RecordingDbx(**dbx_kwargs)
        with mock.patch.object(bootstrap_service, 'DatabricksClient',
                               lambda url, pat: legacy_dbx):
            legacy_result, legacy_progress = self.run_legacy(legacy_dbx)
        with mock.patch.object(bootstrap_service, 'DatabricksClient',
                               lambda url, pat: shared_dbx):
            shared_result, shared_progress = self.run_shared(shared_dbx)
        return (legacy_dbx, legacy_result, legacy_progress,
                shared_dbx, shared_result, shared_progress)

    def test_databricks_call_tapes_are_identical(self):
        legacy_dbx, _, _, shared_dbx, _, _ = self._both()
        self.assertEqual(legacy_dbx.calls, shared_dbx.calls)
        self.assertGreater(len(shared_dbx.calls), 15)

    def test_progress_tapes_are_identical(self):
        _, _, legacy_progress, _, _, shared_progress = self._both()
        self.assertEqual(legacy_progress, shared_progress)

    def test_returned_artefact_ids_match(self):
        _, legacy_result, _, _, shared_result, _ = self._both()
        for key in ('snapshot_pipeline_id', 'cdc_pipeline_id', 'notebook_folder',
                    'volume_path', 'compute_type', 'warehouse_id', 'catalog_name',
                    'snapshot_workflow_id', 'cdc_workflow_id', 'catalog_claim_id'):
            self.assertEqual(legacy_result[key], shared_result[key], key)

    def test_identical_with_the_notebook_download_path_on(self):
        with mock.patch.dict(os.environ, {'ENABLE_NOTEBOOK_DOWNLOAD': 'true'}):
            legacy_dbx, legacy_result, legacy_progress, shared_dbx, shared_result, shared_progress = self._both()
        self.assertEqual(legacy_dbx.calls, shared_dbx.calls)
        self.assertEqual(legacy_progress, shared_progress)
        self.assertEqual(legacy_result['snapshot_workflow_id'], 111)
        self.assertEqual(shared_result['snapshot_workflow_id'], 111)

    def test_identical_when_the_volume_needs_a_retry(self):
        """A freshly created volume can 404 once on the Files API."""
        class FlakyVolume(RecordingDbx):
            def __init__(self):
                super().__init__()
                self._seen = 0

            def validate_volume_path(self, path):
                self._record('validate_volume_path', path)
                self._seen += 1
                return self._seen > 1

        legacy_dbx, shared_dbx = FlakyVolume(), FlakyVolume()
        # The legacy path does a function-local `import time`, so patch the module itself
        # rather than an attribute on either service.
        with mock.patch('time.sleep', lambda s: None):
            with mock.patch.object(bootstrap_service, 'DatabricksClient',
                                   lambda url, pat: legacy_dbx):
                _, legacy_progress = self.run_legacy(legacy_dbx)
            with mock.patch.object(bootstrap_service, 'DatabricksClient',
                                   lambda url, pat: shared_dbx):
                _, shared_progress = self.run_shared(shared_dbx)
        self.assertEqual(legacy_dbx.calls, shared_dbx.calls)
        self.assertEqual(legacy_progress, shared_progress)

    def test_identical_failure_when_no_warehouse_exists(self):
        legacy_dbx = RecordingDbx(warehouses=[])
        shared_dbx = RecordingDbx(warehouses=[])
        with mock.patch.object(bootstrap_service, 'DatabricksClient',
                               lambda url, pat: legacy_dbx):
            with self.assertRaises(RuntimeError) as legacy_err:
                self.run_legacy(legacy_dbx)
        with mock.patch.object(bootstrap_service, 'DatabricksClient',
                               lambda url, pat: shared_dbx):
            with self.assertRaises(RuntimeError) as shared_err:
                self.run_shared(shared_dbx)
        self.assertEqual(str(legacy_err.exception), str(shared_err.exception))
        self.assertEqual(legacy_dbx.calls, shared_dbx.calls)

    def test_identical_failure_when_the_volume_never_appears(self):
        legacy_dbx = RecordingDbx(volume_ok=False)
        shared_dbx = RecordingDbx(volume_ok=False)
        with mock.patch('time.sleep', lambda s: None):
            with mock.patch.object(bootstrap_service, 'DatabricksClient',
                                   lambda url, pat: legacy_dbx):
                with self.assertRaises(RuntimeError) as legacy_err:
                    self.run_legacy(legacy_dbx)
            with mock.patch.object(bootstrap_service, 'DatabricksClient',
                                   lambda url, pat: shared_dbx):
                with self.assertRaises(RuntimeError) as shared_err:
                    self.run_shared(shared_dbx)
        self.assertEqual(str(legacy_err.exception), str(shared_err.exception))


class SharedStepBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.progress = []
        env = mock.patch.dict(os.environ, {'ENABLE_NOTEBOOK_DOWNLOAD': 'false'})
        env.start()
        self.addCleanup(env.stop)

    def record(self, step, message, *, error=None):
        self.progress.append((step, message, error))

    def test_run_all_creates_the_expected_artefacts(self):
        dbx = RecordingDbx()
        result = bootstrap_steps.run_all(dbx, CATALOG, progress=self.record)
        self.assertEqual(result['warehouse_id'], 'wh-1')
        self.assertEqual(
            result['volume_path'], f'/Volumes/{CATALOG}/bronze/acc_bronze_volume',
        )
        self.assertTrue(result['snapshot_pipeline_id'])
        self.assertTrue(result['cdc_pipeline_id'])

    def test_workflows_skipped_when_flag_off_and_not_forced(self):
        dbx = RecordingDbx()
        result = bootstrap_steps.run_all(dbx, CATALOG, progress=self.record)
        self.assertIsNone(result['snapshot_workflow_id'])
        self.assertNotIn(
            'create_combined_snapshot_workflow', [c[0] for c in dbx.calls],
        )

    def test_create_workflows_true_forces_them_even_with_the_flag_off(self):
        """The M2M driver passes True so flipping the fleet flag cannot strand connections."""
        dbx = RecordingDbx()
        result = bootstrap_steps.run_all(
            dbx, CATALOG, progress=self.record, create_workflows=True,
        )
        self.assertEqual(result['snapshot_workflow_id'], 111)
        self.assertEqual(result['cdc_workflow_id'], 222)
        self.assertIn('create_combined_snapshot_workflow', [c[0] for c in dbx.calls])

    def test_uploads_every_notebook_and_the_shared_helpers(self):
        dbx = RecordingDbx()
        bootstrap_steps.run_all(dbx, CATALOG, progress=self.record)
        uploaded = [c[1][0] for c in dbx.calls if c[0] == 'import_notebook']
        for name in bootstrap_steps.NOTEBOOK_NAMES:
            self.assertIn(f'/Users/alice@acme.com/acc/v1/{name}', uploaded)
        self.assertTrue(any('/shared/' in p for p in uploaded))

    def test_creates_all_three_meta_tables(self):
        dbx = RecordingDbx()
        bootstrap_steps.run_all(dbx, CATALOG, progress=self.record)
        sql = ' '.join(c[1][1] for c in dbx.calls if c[0] == 'execute_sql')
        for table in (bootstrap_steps.META_REGISTRY_TABLE,
                      bootstrap_steps.META_STATUS_TABLE,
                      bootstrap_steps.META_SCHEMA_VERSIONS_TABLE):
            self.assertIn(table, sql)

    def test_pipelines_are_catalog_scoped_and_serverless_advanced(self):
        dbx = RecordingDbx()
        bootstrap_steps.run_all(dbx, CATALOG, progress=self.record)
        pipelines = [c for c in dbx.calls if c[0] == 'get_or_create_pipeline']
        self.assertEqual(len(pipelines), 2)
        for call in pipelines:
            self.assertEqual(call[1][1], CATALOG)
            self.assertEqual(call[1][2], 'ADVANCED')

    def test_progress_reports_a_failing_step_before_raising(self):
        class Broken(RecordingDbx):
            def uc_create_schema(self, catalog, schema):
                raise RuntimeError('permission denied')

        with self.assertRaises(RuntimeError):
            bootstrap_steps.run_all(Broken(), CATALOG, progress=self.record)
        last = self.progress[-1]
        self.assertEqual(last[0], 5)
        self.assertIn('Schema creation failed', last[2])

    def test_steps_are_individually_callable(self):
        """The M2M driver reuses individual steps for resume, not just run_all."""
        dbx = RecordingDbx()
        bootstrap_steps.validate_workspace(dbx, self.record)
        self.assertEqual(bootstrap_steps.ensure_warehouse_running(dbx, self.record), 'wh-1')


class KillSwitchTests(unittest.TestCase):
    def test_defaults_to_the_legacy_path(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('USE_SHARED_STEPS', None)
            self.assertFalse(bootstrap_service.use_shared_steps())

    def test_enabled_by_env(self):
        with mock.patch.dict(os.environ, {'USE_SHARED_STEPS': 'true'}):
            self.assertTrue(bootstrap_service.use_shared_steps())

    def test_dispatch_follows_the_flag(self):
        with mock.patch.object(bootstrap_service, '_provision_catalog_shared') as shared, \
             mock.patch.object(bootstrap_service, '_provision_catalog_legacy') as legacy:
            with mock.patch.dict(os.environ, {'USE_SHARED_STEPS': 'true'}):
                bootstrap_service._provision_catalog('u', 'w', 'p', CATALOG, CLAIM)
            shared.assert_called_once()
            legacy.assert_not_called()

            shared.reset_mock()
            with mock.patch.dict(os.environ, {'USE_SHARED_STEPS': 'false'}):
                bootstrap_service._provision_catalog('u', 'w', 'p', CATALOG, CLAIM)
            legacy.assert_called_once()
            shared.assert_not_called()


if __name__ == '__main__':
    unittest.main()
