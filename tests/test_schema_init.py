"""
Purpose: The multi-tenant model is only safe because the database refuses the duplicates —
two robots for one hub, two tenants for one hub, a shared robot across hubs. These tests run
init_db() against a temp SQLite file and assert every FR-05 §6.1 constraint actually fires,
plus that the shipped U2M tables are untouched. Traces TC-SSA-09, TC-USER-08, TC-CONN-10,
TC-CONC-04/05/06 and FR-04 TC-08.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')

from backend.repositories.state import database  # noqa: E402

NEW_TABLES = (
    'aps_apps',
    'tenants',
    'tenant_users',
    'ssa_credentials',
    'connections',
    'connection_dbx_credentials',
    'connection_bootstrap',
    'connection_sync_runs',
    'connection_watermarks',
)

# Phase 0 tables the U2M path depends on. Phase 1 must not disturb them.
U2M_TABLES = (
    'acc_tokens',
    'acc_config',
    'dbx_credentials',
    'dbx_tokens',
    'bootstrap_state',
    'watermarks',
    'sync_runs',
    'app_secrets',
    'catalog_claims',
)


class _TempDbTestCase(unittest.TestCase):
    """Point database.DB_PATH at a throwaway file and build the schema."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='schema-test-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.db_path = os.path.join(self.tmpdir, 'connector.db')
        patcher = mock.patch.object(database, 'DB_PATH', self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No APS_CLIENT_ID by default so seeding is opt-in per test.
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('APS_CLIENT_ID', None)
        database.init_db()

    def tables(self) -> set[str]:
        with database._conn() as con:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        return {r['name'] for r in rows}

    def columns(self, table: str) -> set[str]:
        with database._conn() as con:
            rows = con.execute(f'PRAGMA table_info({table})').fetchall()
        return {r[1] for r in rows}

    def execute(self, sql, params=()):
        with database._conn() as con:
            con.execute(sql, params)

    def seed_app(self, app_ref='app_a', client_id='AAA'):
        self.execute(
            'INSERT INTO aps_apps (app_ref, client_id, client_secret_ref, max_robots,'
            ' is_active, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (app_ref, client_id, f'ref/{app_ref}', 10, 1, time.time()),
        )

    def seed_tenant(self, hub_id='b.hub1', app_ref='app_a'):
        self.execute(
            'INSERT INTO tenants (tenant_id, hub_id, aps_app_ref, onboarding_status,'
            ' created_at) VALUES (?, ?, ?, ?, ?)',
            (hub_id, hub_id, app_ref, 'pending_whitelist', time.time()),
        )


class SchemaCreationTests(_TempDbTestCase):
    def test_all_new_tables_created(self):
        present = self.tables()
        for table in NEW_TABLES:
            self.assertIn(table, present)

    def test_u2m_tables_untouched(self):
        present = self.tables()
        for table in U2M_TABLES:
            self.assertIn(table, present)

    def test_init_db_is_idempotent(self):
        database.init_db()
        database.init_db()
        for table in NEW_TABLES:
            self.assertIn(table, self.tables())

    def test_connection_bootstrap_carries_the_workflow_and_job_ids(self):
        """FR-05 §5.7 omits these, but the shipped pipeline cannot run without them."""
        cols = self.columns('connection_bootstrap')
        for col in (
            'catalog_name',
            'snapshot_workflow_id',
            'cdc_workflow_id',
            'download_job_id',
            'catalog_claim_id',
            'volume_path',
            'warehouse_id',
            'notebook_folder',
        ):
            self.assertIn(col, cols)

    def test_connections_has_next_run_at_not_cron(self):
        cols = self.columns('connections')
        self.assertIn('auto_cdc_next_run_at', cols)
        self.assertIn('cdc_enabled', cols)
        self.assertNotIn('schedule_cron', cols)

    def test_sync_runs_carry_failure_attribution(self):
        """FR-02 FR-27 — a failure must name the connection, the run and the phase."""
        cols = self.columns('connection_sync_runs')
        for col in ('connection_id', 'run_id', 'phase', 'mode', 'trigger_type', 'error'):
            self.assertIn(col, cols)

    def test_ssa_credentials_never_holds_key_material(self):
        """Tier T3: the PEM lives in the SecretStore; the row holds only a ref."""
        cols = self.columns('ssa_credentials')
        self.assertIn('private_key_ref', cols)
        for forbidden in ('private_key', 'private_key_pem', 'pem', 'client_secret'):
            self.assertNotIn(forbidden, cols)

    def test_dbx_credentials_never_holds_the_secret(self):
        cols = self.columns('connection_dbx_credentials')
        self.assertIn('client_secret_ref', cols)
        self.assertNotIn('client_secret', cols)

    def test_aps_apps_never_holds_the_secret(self):
        cols = self.columns('aps_apps')
        self.assertIn('client_secret_ref', cols)
        self.assertNotIn('client_secret', cols)


class UniqueConstraintTests(_TempDbTestCase):
    def test_one_tenant_per_hub(self):
        """TC-CONC-04 / TC-SSA-09."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute(
                'INSERT INTO tenants (tenant_id, hub_id, aps_app_ref, onboarding_status,'
                ' created_at) VALUES (?, ?, ?, ?, ?)',
                ('other-pk', 'b.hub1', 'app_a', 'pending_whitelist', time.time()),
            )

    def test_one_user_mapping_per_hub_user_pair(self):
        """TC-USER-08 / TC-CONC-05."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        row = ('b.hub1', 'user-1', 'hub_admin', time.time())
        self.execute(
            'INSERT INTO tenant_users (tenant_id, aps_user_id, role, created_at)'
            ' VALUES (?, ?, ?, ?)', row,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute(
                'INSERT INTO tenant_users (tenant_id, aps_user_id, role, created_at)'
                ' VALUES (?, ?, ?, ?)', row,
            )

    def _insert_ssa(self, hub_id, service_account_id, tenant_id=None):
        self.execute(
            'INSERT INTO ssa_credentials (tenant_id, hub_id, aps_app_ref,'
            ' service_account_id, robot_email, key_id, private_key_ref, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                tenant_id or hub_id, hub_id, 'app_a', service_account_id,
                f'{service_account_id}@x.adskserviceaccount.autodesk.com',
                'kid-1', f'ref/hub/{hub_id}/ssa-private-key', time.time(),
            ),
        )

    def test_one_ssa_per_hub(self):
        """TC-CONC-06 / TC-SSA-01."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        self._insert_ssa('b.hub1', 'svc-1')
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_ssa('b.hub1', 'svc-2', tenant_id='another')

    def test_service_account_cannot_be_shared_across_hubs(self):
        """FR-04 TC-08 — the anti-pattern guard. UNIQUE(service_account_id)."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        self.seed_tenant('b.hub2')
        self._insert_ssa('b.hub1', 'svc-shared')
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_ssa('b.hub2', 'svc-shared')

    def _insert_connection(self, connection_id, hub='b.hub1', project='p1',
                           workspace='https://w1', catalog='c1'):
        self.execute(
            'INSERT INTO connections (connection_id, tenant_id, hub_id, project_id,'
            ' dbx_workspace_url, catalog, onboarding_status, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (connection_id, hub, hub, project, workspace, catalog,
             'pending_custom_integration', time.time()),
        )

    def test_one_connection_per_quadruple(self):
        """TC-CONN-01 / TC-CONN-10 / TC-CONC-03."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        self._insert_connection('cnx_a')
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_connection('cnx_b')

    def test_different_project_is_a_different_connection(self):
        """TC-CONN-03 — same hub and target, new project, must be allowed."""
        self.seed_app()
        self.seed_tenant('b.hub1')
        self._insert_connection('cnx_a', project='p1')
        self._insert_connection('cnx_b', project='p2')

    def test_one_registry_row_per_aps_client_id(self):
        self.seed_app('app_a', 'AAA')
        with self.assertRaises(sqlite3.IntegrityError):
            self.seed_app('app_b', 'AAA')

    def test_one_watermark_per_connection_and_type(self):
        self.seed_app()
        self.seed_tenant('b.hub1')
        self._insert_connection('cnx_a')
        row = ('cnx_a', 'data_connector', time.time())
        self.execute(
            'INSERT INTO connection_watermarks (connection_id, data_type, last_sync)'
            ' VALUES (?, ?, ?)', row,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute(
                'INSERT INTO connection_watermarks (connection_id, data_type, last_sync)'
                ' VALUES (?, ?, ?)', row,
            )

    def test_sync_run_id_autoincrements(self):
        self.seed_app()
        self.seed_tenant('b.hub1')
        self._insert_connection('cnx_a')
        now = time.time()
        with database._conn() as con:
            for _ in range(2):
                con.execute(
                    'INSERT INTO connection_sync_runs (connection_id, trigger_type, mode,'
                    ' state, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                    ('cnx_a', 'manual', 'snapshot', 'pending', now, now),
                )
            ids = [
                r['run_id'] for r in con.execute(
                    'SELECT run_id FROM connection_sync_runs ORDER BY run_id'
                ).fetchall()
            ]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])


class ApsAppSeedTests(_TempDbTestCase):
    def test_no_seed_without_aps_client_id(self):
        with database._conn() as con:
            rows = con.execute('SELECT COUNT(*) AS n FROM aps_apps').fetchone()
        self.assertEqual(rows['n'], 0)

    def test_seeds_app_a_from_env(self):
        with mock.patch.dict(os.environ, {'APS_CLIENT_ID': 'AAA-from-myapps'}):
            database.init_db()
            with database._conn() as con:
                row = con.execute(
                    'SELECT * FROM aps_apps WHERE app_ref = ?', ('app_a',)
                ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['client_id'], 'AAA-from-myapps')
        self.assertEqual(row['max_robots'], 10)
        self.assertEqual(row['is_active'], 1)
        self.assertIn('aps-app-app_a-secret', row['client_secret_ref'])

    def test_seed_does_not_overwrite_an_existing_row(self):
        """A rotated Client ID in the DB must survive a restart with a stale .env."""
        self.seed_app('app_a', 'ROTATED')
        with mock.patch.dict(os.environ, {'APS_CLIENT_ID': 'AAA-old'}):
            database.init_db()
            with database._conn() as con:
                row = con.execute(
                    'SELECT client_id FROM aps_apps WHERE app_ref = ?', ('app_a',)
                ).fetchone()
        self.assertEqual(row['client_id'], 'ROTATED')

    def test_seed_is_idempotent(self):
        with mock.patch.dict(os.environ, {'APS_CLIENT_ID': 'AAA'}):
            database.init_db()
            database.init_db()
            with database._conn() as con:
                n = con.execute('SELECT COUNT(*) AS n FROM aps_apps').fetchone()['n']
        self.assertEqual(n, 1)


if __name__ == '__main__':
    unittest.main()
