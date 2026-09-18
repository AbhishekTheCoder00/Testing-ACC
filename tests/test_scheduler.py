"""
Purpose: The scheduler is what makes sync unattended, so its failure modes matter more than
its happy path: one broken connection must not abort the tick for everyone else, a connection
already syncing must be skipped rather than doubled up, and a permanently failing connection
must not become a hot retry loop. Traces FR-03 §11.4/§13/§15.2, TC-SYNC-07, BO-05.
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
from backend.services.m2m import connection_service as svc  # noqa: E402
from backend.services.m2m import scheduler  # noqa: E402

HUB = 'b.hub1'
WS = 'https://dbc-abc123.cloud.databricks.com'


class SchedulerTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        self.calls = []
        patcher = mock.patch.object(
            scheduler.connection_runner, 'run_connection_sync',
            side_effect=self._record,
        )
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, connection_id, **kwargs):
        self.calls.append((connection_id, kwargs))
        return {'run_id': len(self.calls), 'state': 'complete'}

    def make(self, project='p1', catalog='c1', *, due_at=0.0, sp=True, ready=True,
             enabled=True):
        connection = svc.create_or_resume(
            hub_id=HUB, project_id=project, dbx_workspace_url=WS, catalog=catalog,
        ).connection
        cid = connection['connection_id']
        if sp:
            conns.save_dbx_credentials(
                cid, workspace_url=WS, client_id=f'sp-{project}',
                client_secret_ref=f'ref/{project}', validated_at=1.0,
            )
        if ready:
            conns.update_status(cid, 'ready')
        conns.set_schedule(cid, enabled=enabled, next_run_at=due_at)
        return cid

    def test_runs_a_due_connection_as_a_scheduled_cdc_sync(self):
        cid = self.make()
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [(cid, {'mode': 'cdc', 'trigger_type': 'scheduled'})])
        self.assertEqual(result['started'], [cid])
        self.assertEqual(result['skipped'], [])
        self.assertEqual(result['failed'], [])

    def test_advances_the_next_run_before_syncing(self):
        """Claiming the slot up front stops two overlapping ticks running one connection
        twice, and stops a long run from drifting the schedule."""
        cid = self.make()
        seen = {}

        def capture(connection_id, **_kw):
            seen['next_run_at'] = conns.get(connection_id)['auto_cdc_next_run_at']
            return {'state': 'complete'}

        self.sync.side_effect = capture
        scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(seen['next_run_at'], 1_000.0 + svc.CDC_INTERVAL_SEC)

    def test_a_connection_that_is_not_due_yet_is_left_alone(self):
        self.make(due_at=9_999_999.0)
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['started'], [])

    def test_a_connection_with_cdc_disabled_is_left_alone(self):
        self.make(enabled=False)
        scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [])

    def test_a_connection_without_a_service_principal_is_never_picked_up(self):
        """D-4/BR-08 — a cron tick may not borrow a human's Databricks token."""
        self.make(sp=False)
        scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [])

    def test_an_unbootstrapped_connection_is_never_picked_up(self):
        self.make(ready=False)
        scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [])

    def test_an_in_flight_connection_is_skipped_and_recorded(self):
        """TC-SYNC-07 — a manual sync in progress must not be doubled up."""
        cid = self.make()
        runs.create_run(cid, mode='snapshot', trigger_type='manual')
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['skipped'], [cid])

    def test_a_skipped_connection_is_retried_on_the_next_tick(self):
        cid = self.make()
        run_id = runs.create_run(cid, mode='snapshot', trigger_type='manual')
        scheduler.scheduled_cdc_tick(now=1_000.0)
        runs.update_run(run_id, 'success')
        result = scheduler.scheduled_cdc_tick(now=1_100.0)
        self.assertEqual(result['started'], [cid])

    def test_one_failure_does_not_abort_the_tick(self):
        first = self.make(project='p1', catalog='c1')
        second = self.make(project='p2', catalog='c2')

        def explode(connection_id, **_kw):
            if connection_id == first:
                raise RuntimeError('DC export refused')
            return {'state': 'complete'}

        self.sync.side_effect = explode
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(result['failed'], [first])
        self.assertEqual(result['started'], [second])

    def test_a_failing_connection_still_has_its_schedule_advanced(self):
        """Otherwise a permanently broken connection becomes a hot retry loop."""
        cid = self.make()
        self.sync.side_effect = RuntimeError('boom')
        scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(
            conns.get(cid)['auto_cdc_next_run_at'], 1_000.0 + svc.CDC_INTERVAL_SEC,
        )

    def test_a_null_next_run_is_treated_as_due_immediately(self):
        cid = self.make()
        conns.set_schedule(cid, enabled=True, next_run_at=None)
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(result['started'], [cid])

    def test_an_empty_tick_is_not_an_error(self):
        result = scheduler.scheduled_cdc_tick(now=1_000.0)
        self.assertEqual(result, {'started': [], 'skipped': [], 'failed': [], 'due': 0})


if __name__ == '__main__':
    unittest.main()
