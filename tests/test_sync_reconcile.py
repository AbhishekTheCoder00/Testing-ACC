"""Tests for orphaned sync run reconciliation against Databricks."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.sync import sync_reconcile_service as reconcile  # noqa: E402


class TestReconcileWorkflowRun(unittest.TestCase):
    def setUp(self):
        reconcile._last_reconcile_at.clear()

    @patch('backend.services.sync.sync_reconcile_service.create_databricks_client_for_user')
    @patch('backend.services.sync.sync_reconcile_service.commit_successful_watermarks_u2m')
    @patch('backend.services.sync.sync_reconcile_service.db.update_sync_run')
    @patch('backend.services.sync.sync_reconcile_service.db.get_bootstrap_state')
    @patch('backend.services.sync.sync_reconcile_service.db.get_sync_run')
    def test_marks_complete_when_workflow_succeeded(
        self,
        mock_get_run,
        mock_bs,
        mock_update,
        mock_watermarks,
        mock_client,
    ):
        run = {
            'run_id': 42,
            'project_id': 'b.proj1',
            'state': 'workflow_running',
            'bronze_run_id': 999,
            'trigger_type': 'manual',
            'data_types': 'data_connector',
            'started_at': time.time() - 3600,
        }
        completed = {**run, 'state': 'complete'}
        mock_bs.return_value = {'snapshot_pipeline_id': 'p1', 'cdc_pipeline_id': 'p2'}
        mock_get_run.return_value = completed
        dbx = MagicMock()
        dbx.get_workflow_run_state.return_value = ('TERMINATED', 'SUCCESS')
        dbx._WORKFLOW_TERMINAL_LC = frozenset({'TERMINATED', 'SKIPPED', 'INTERNAL_ERROR'})
        mock_client.return_value = dbx

        result = reconcile.reconcile_u2m_sync_run('user1', run)

        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][1], 'complete')
        mock_watermarks.assert_called_once()
        self.assertEqual(result['state'], 'complete')

    @patch('backend.services.sync.sync_reconcile_service.create_databricks_client_for_user')
    @patch('backend.services.sync.sync_reconcile_service.db.get_bootstrap_state')
    def test_no_op_while_workflow_still_running(self, mock_bs, mock_client):
        run = {
            'run_id': 43,
            'project_id': 'b.proj1',
            'state': 'workflow_running',
            'bronze_run_id': 1000,
            'data_types': 'data_connector',
            'started_at': time.time() - 60,
        }
        mock_bs.return_value = {'snapshot_pipeline_id': 'p1'}
        dbx = MagicMock()
        dbx.get_workflow_run_state.return_value = ('RUNNING', '')
        mock_client.return_value = dbx

        result = reconcile.reconcile_u2m_sync_run('user1', run)

        self.assertEqual(result['state'], 'workflow_running')

    @patch('backend.services.sync.sync_reconcile_service.create_databricks_client_for_user')
    @patch('backend.services.sync.sync_reconcile_service.db.update_sync_run')
    @patch('backend.services.sync.sync_reconcile_service.db.get_bootstrap_state')
    @patch('backend.services.sync.sync_reconcile_service.db.get_sync_run')
    def test_marks_failed_when_workflow_failed(
        self,
        mock_get_run,
        mock_bs,
        mock_update,
        mock_client,
    ):
        run = {
            'run_id': 44,
            'project_id': 'b.proj1',
            'state': 'workflow_running',
            'bronze_run_id': 1001,
            'data_types': 'data_connector',
            'started_at': time.time() - 3600,
        }
        failed = {**run, 'state': 'failed'}
        mock_bs.return_value = {'snapshot_pipeline_id': 'p1'}
        mock_get_run.return_value = failed
        dbx = MagicMock()
        dbx.get_workflow_run_state.return_value = ('TERMINATED', 'FAILED')
        dbx._WORKFLOW_TERMINAL_LC = frozenset({'TERMINATED', 'SKIPPED', 'INTERNAL_ERROR'})
        mock_client.return_value = dbx

        result = reconcile.reconcile_u2m_sync_run('user1', run)

        mock_update.assert_called_once()
        self.assertEqual(mock_update.call_args[0][1], 'failed')
        self.assertEqual(result['state'], 'failed')

    def test_skips_terminal_runs(self):
        run = {'run_id': 45, 'state': 'complete', 'project_id': 'b.proj1'}
        result = reconcile.reconcile_u2m_sync_run('user1', run)
        self.assertEqual(result['state'], 'complete')

    def test_throttles_rapid_reconcile_attempts(self):
        reconcile._last_reconcile_at[99] = time.time()
        run = {
            'run_id': 99,
            'project_id': 'b.proj1',
            'state': 'workflow_running',
            'bronze_run_id': 1,
            'data_types': 'data_connector',
        }
        with patch(
            'backend.services.sync.sync_reconcile_service.create_databricks_client_for_user',
        ) as mock_client:
            result = reconcile.reconcile_u2m_sync_run('user1', run)
            mock_client.assert_not_called()
            self.assertEqual(result['state'], 'workflow_running')


if __name__ == '__main__':
    unittest.main()
