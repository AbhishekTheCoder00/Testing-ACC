"""
Purpose: The connection API is the portal's whole surface, so these tests drive it through
Flask's test client. The ones that matter: a hub admin cannot reach another hub's connection by
editing the URL, a service-principal secret is accepted but never echoed back, long work returns
202 and runs off-request, a second start while one is in flight is 409, and scheduling refuses
without a service principal. Traces FR-03 §13, TC-CONN-*, TC-SYNC-08/10, TC-AUTH-05, NFR-01.
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
from backend.routes import connection_routes  # noqa: E402
from backend.secrets import get_secret_store  # noqa: E402
from backend.services.m2m import connection_service as svc  # noqa: E402
from backend.services.m2m.provisioning_probe import ProbeResult  # noqa: E402

HUB = 'b.hub-aaa'
OTHER_HUB = 'b.hub-bbb'
WS = 'https://dbc-abc123.cloud.databricks.com'


class _RouteTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a', max_robots=10)
        tenants.ensure_tenant(HUB, 'app_a')
        tenants.ensure_tenant(OTHER_HUB, 'app_a')
        tenants.add_user(HUB, 'alice')
        tenants.add_user(OTHER_HUB, 'bob')

        import app as flask_app

        flask_app.app.config['TESTING'] = True
        self.client = flask_app.app.test_client()

        # Routes must never do the work inline; every test asserts against these fakes.
        self.threads = []
        patcher = mock.patch.object(
            connection_routes, '_start_thread',
            side_effect=lambda fn, *a, **kw: self.threads.append((fn, a, kw)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def sign_in(self, user_id='alice', hub_id=HUB):
        with self.client.session_transaction() as sess:
            sess['user_id'] = user_id
            if hub_id:
                sess['tenant_id'] = hub_id

    def make(self, project='p1', catalog='c1', hub=HUB, *, ready=False, sp=False):
        connection = svc.create_or_resume(
            hub_id=hub, project_id=project, dbx_workspace_url=WS, catalog=catalog,
        ).connection
        cid = connection['connection_id']
        if sp:
            conns.save_dbx_credentials(
                cid, workspace_url=WS, client_id='sp-1',
                client_secret_ref='ref/sp-1', validated_at=1.0,
            )
        if ready:
            conns.set_pipeline_ids(cid, snapshot_pipeline_id='snap-1',
                                   cdc_pipeline_id='cdc-1')
            conns.save_bootstrap(cid, catalog_name=catalog,
                                 volume_path='/Volumes/c1/bronze/acc_bronze_volume',
                                 warehouse_id='wh-1')
            conns.update_status(cid, 'ready')
        return cid


class AuthTests(_RouteTestCase):
    def test_listing_requires_a_signed_in_user(self):
        self.assertEqual(self.client.get('/api/connections').status_code, 401)

    def test_listing_requires_a_selected_hub(self):
        self.sign_in(hub_id=None)
        self.assertEqual(self.client.get('/api/connections').status_code, 400)

    def test_a_non_member_of_the_active_hub_is_refused(self):
        self.sign_in('carol')
        self.assertEqual(self.client.get('/api/connections').status_code, 403)

    def test_another_hubs_connection_is_not_found_rather_than_forbidden(self):
        """TC-AUTH-05 — the response must not confirm that another hub's id exists."""
        other = self.make(hub=OTHER_HUB, project='p9', catalog='c9')
        self.sign_in()
        resp = self.client.get(f'/api/connections/{other}')
        self.assertEqual(resp.status_code, 404)

    def test_a_hub_only_sees_its_own_connections(self):
        mine = self.make(project='p1', catalog='c1')
        self.make(hub=OTHER_HUB, project='p9', catalog='c9')
        self.sign_in()
        body = self.client.get('/api/connections').get_json()
        self.assertEqual([c['connection_id'] for c in body['connections']], [mine])


class CreateTests(_RouteTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def post(self, **payload):
        body = {
            'project_id': 'p1', 'project_name': 'Riverside',
            'workspace_url': WS, 'catalog': 'c1',
        }
        body.update(payload)
        return self.client.post('/api/connections', json=body)

    def test_creates_a_connection_for_the_active_hub(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertFalse(body['resumed'])
        self.assertEqual(body['connection']['hub_id'], HUB)
        self.assertEqual(body['next_step'], 'whitelist')

    def test_the_same_target_resumes_with_200(self):
        self.post()
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['resumed'])

    def test_a_missing_field_is_a_400(self):
        resp = self.post(catalog='')
        self.assertEqual(resp.status_code, 400)

    def test_a_catalog_owned_by_someone_else_is_a_409_naming_the_catalog(self):
        from backend.repositories.state import catalog_claim_repository as claim_repo

        claim_repo.claim_catalog(claim_repo.normalize_workspace_key(WS), 'c1', 'user-42')
        resp = self.post()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['reason'], 'catalog_taken')
        self.assertIn('c1', resp.get_json()['error'])

    def test_service_principal_credentials_are_vaulted_and_never_echoed(self):
        """NFR-01 — the secret goes to the vault; only the ref and client id persist."""
        resp = self.post(dbx_client_id='sp-1', dbx_client_secret='super-secret')
        self.assertEqual(resp.status_code, 201)
        raw = resp.get_data(as_text=True)
        self.assertNotIn('super-secret', raw)

        cid = resp.get_json()['connection']['connection_id']
        stored = conns.get_dbx_credentials(cid)
        self.assertEqual(stored['client_id'], 'sp-1')
        self.assertNotIn('super-secret', str(stored))
        self.assertEqual(get_secret_store().get_secret(stored['client_secret_ref']),
                         'super-secret')

    def test_a_client_id_without_a_secret_is_rejected(self):
        resp = self.post(dbx_client_id='sp-1')
        self.assertEqual(resp.status_code, 400)


class DbxCredentialTests(_RouteTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make()
        self.sign_in()

    def test_adding_credentials_stores_the_secret_in_the_vault_only(self):
        resp = self.client.post(
            f'/api/connections/{self.cid}/dbx-credentials',
            json={'client_id': 'sp-9', 'client_secret': 'hunter2'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('hunter2', resp.get_data(as_text=True))
        stored = conns.get_dbx_credentials(self.cid)
        self.assertEqual(stored['client_id'], 'sp-9')
        self.assertEqual(get_secret_store().get_secret(stored['client_secret_ref']),
                         'hunter2')

    def test_the_response_reports_only_that_a_principal_exists(self):
        self.client.post(
            f'/api/connections/{self.cid}/dbx-credentials',
            json={'client_id': 'sp-9', 'client_secret': 'hunter2'},
        )
        body = self.client.get(f'/api/connections/{self.cid}').get_json()
        self.assertTrue(body['has_service_principal'])
        self.assertNotIn('client_secret', str(body))
        self.assertNotIn('client_secret_ref', str(body))

    def test_a_missing_secret_is_a_400(self):
        resp = self.client.post(
            f'/api/connections/{self.cid}/dbx-credentials', json={'client_id': 'sp-9'},
        )
        self.assertEqual(resp.status_code, 400)

    def test_rotating_replaces_the_stored_secret(self):
        for secret in ('first', 'second'):
            self.client.post(
                f'/api/connections/{self.cid}/dbx-credentials',
                json={'client_id': 'sp-9', 'client_secret': secret},
            )
        stored = conns.get_dbx_credentials(self.cid)
        self.assertEqual(get_secret_store().get_secret(stored['client_secret_ref']),
                         'second')


class BootstrapTests(_RouteTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make(sp=True)
        self.sign_in()

    def test_bootstrap_returns_202_and_runs_off_request(self):
        """ARCHITECTURE — long work never blocks the request thread."""
        resp = self.client.post(f'/api/connections/{self.cid}/bootstrap')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(self.threads), 1)

    def test_bootstrap_is_refused_while_one_is_running(self):
        from backend.services.m2m import connection_bootstrap as cb

        self.assertTrue(cb.try_begin(self.cid))
        self.addCleanup(cb.end, self.cid)
        resp = self.client.post(f'/api/connections/{self.cid}/bootstrap')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.threads, [])

    def test_bootstrap_status_reports_progress(self):
        from backend.services.m2m import connection_bootstrap as cb

        cb.set_progress(self.cid, 4, 'Verifying catalog: c1...')
        self.addCleanup(cb.reset_progress, self.cid)
        body = self.client.get(f'/api/connections/{self.cid}/bootstrap/status').get_json()
        self.assertEqual(body['step'], 4)
        self.assertFalse(body['done'])
        self.assertEqual(body['onboarding_status'], 'pending_custom_integration')

    def test_bootstrap_without_a_service_principal_uses_the_users_databricks_token(self):
        conns.delete(self.cid)
        cid = self.make(project='p2', catalog='c2')
        with mock.patch.object(connection_routes, 'get_valid_dbx_token',
                               return_value={'access_token': 'u2m-token'}):
            resp = self.client.post(f'/api/connections/{cid}/bootstrap')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.threads[0][2]['user_token'], 'u2m-token')

    def test_bootstrap_without_any_databricks_credential_is_a_400(self):
        conns.delete(self.cid)
        cid = self.make(project='p2', catalog='c2')
        with mock.patch.object(connection_routes, 'get_valid_dbx_token',
                               side_effect=RuntimeError('no token')):
            resp = self.client.post(f'/api/connections/{cid}/bootstrap')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.threads, [])


class SyncTests(_RouteTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make(ready=True, sp=True)
        self.sign_in()

    def test_snapshot_sync_returns_202_and_starts_a_thread(self):
        resp = self.client.post(f'/api/connections/{self.cid}/sync')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.threads[0][2]['mode'], 'snapshot')
        self.assertEqual(self.threads[0][2]['trigger_type'], 'manual')

    def test_cdc_sync_uses_the_cdc_mode(self):
        resp = self.client.post(f'/api/connections/{self.cid}/sync/cdc')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.threads[0][2]['mode'], 'cdc')

    def test_a_second_sync_while_one_is_in_flight_is_a_409(self):
        """TC-SYNC-10 — one connection, one run."""
        runs.create_run(self.cid, mode='snapshot', trigger_type='manual')
        resp = self.client.post(f'/api/connections/{self.cid}/sync')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.threads, [])

    def test_syncing_an_unbootstrapped_connection_is_a_400(self):
        conns.update_status(self.cid, 'pending_bootstrap')
        resp = self.client.post(f'/api/connections/{self.cid}/sync')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.threads, [])

    def test_sync_status_reports_the_latest_run(self):
        run_id = runs.create_run(self.cid, mode='cdc', trigger_type='scheduled')
        runs.update_run(run_id, 'exporting', phase='dc_export')
        body = self.client.get(f'/api/connections/{self.cid}/sync/status').get_json()
        self.assertEqual(body['run']['run_id'], run_id)
        self.assertEqual(body['run']['state'], 'exporting')
        self.assertTrue(body['in_flight'])

    def test_sync_status_with_no_history_is_not_an_error(self):
        body = self.client.get(f'/api/connections/{self.cid}/sync/status').get_json()
        self.assertIsNone(body['run'])
        self.assertFalse(body['in_flight'])

    def test_run_history_is_newest_first_and_capped(self):
        for _ in range(3):
            runs.create_run(self.cid, mode='cdc', trigger_type='scheduled')
        body = self.client.get(f'/api/connections/{self.cid}/runs?limit=2').get_json()
        self.assertEqual(len(body['runs']), 2)
        self.assertGreater(body['runs'][0]['run_id'], body['runs'][1]['run_id'])


class ScheduleTests(_RouteTestCase):
    def setUp(self):
        super().setUp()
        self.cid = self.make(ready=True, sp=True)
        self.sign_in()

    def put(self, enabled):
        return self.client.put(f'/api/connections/{self.cid}/schedule',
                               json={'enabled': enabled})

    def test_enabling_requires_a_passing_robot_probe(self):
        with mock.patch.object(
            connection_routes.connection_service.provisioning_probe,
            'verify_robot_on_project',
            return_value=ProbeResult(True, 'ok', 'verified'),
        ):
            resp = self.put(True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['connection']['cdc_enabled'])

    def test_enabling_without_a_service_principal_is_a_409_that_names_the_fix(self):
        """D-4 — the refusal must be actionable, not a generic error."""
        from backend.repositories.state.database import _conn

        with _conn() as con:
            con.execute('DELETE FROM connection_dbx_credentials WHERE connection_id = ?',
                        (self.cid,))
        resp = self.put(True)
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual(body['reason'], 'missing_service_principal')
        self.assertIn('service principal', body['remediation'].lower())

    def test_enabling_with_the_robot_off_the_project_is_a_409_naming_the_robot(self):
        failure = ProbeResult(
            False, 'robot_not_on_project', 'The service account cannot see this project.',
            remediation='In ACC, invite forma-dbx-1@example.com to this project.',
        )
        with mock.patch.object(
            connection_routes.connection_service.provisioning_probe,
            'verify_robot_on_project', return_value=failure,
        ):
            resp = self.put(True)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('forma-dbx-1@example.com', resp.get_json()['remediation'])

    def test_disabling_always_works(self):
        resp = self.put(False)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['connection']['cdc_enabled'])

    def test_a_non_boolean_enabled_is_a_400(self):
        resp = self.client.put(f'/api/connections/{self.cid}/schedule', json={})
        self.assertEqual(resp.status_code, 400)


class SchedulerEndpointTests(_RouteTestCase):
    def test_the_tick_needs_the_internal_token(self):
        with mock.patch.dict('os.environ', {'INTERNAL_SCHEDULER_TOKEN': 'shhh'}):
            resp = self.client.post('/internal/scheduler/cdc')
            self.assertEqual(resp.status_code, 401)
            resp = self.client.post('/internal/scheduler/cdc',
                                    headers={'X-Internal-Token': 'wrong'})
            self.assertEqual(resp.status_code, 401)

    def test_the_tick_runs_with_the_right_token(self):
        with mock.patch.dict('os.environ', {'INTERNAL_SCHEDULER_TOKEN': 'shhh'}):
            with mock.patch.object(
                connection_routes.scheduler, 'scheduled_cdc_tick',
                return_value={'started': [], 'skipped': [], 'failed': [], 'due': 0},
            ) as tick:
                resp = self.client.post('/internal/scheduler/cdc',
                                        headers={'X-Internal-Token': 'shhh'})
        self.assertEqual(resp.status_code, 200)
        tick.assert_called_once_with()

    def test_the_tick_is_refused_when_no_token_is_configured(self):
        """Fail closed: an unset token must not mean "open to the internet"."""
        with mock.patch.dict('os.environ', {}, clear=False):
            import os

            os.environ.pop('INTERNAL_SCHEDULER_TOKEN', None)
            resp = self.client.post('/internal/scheduler/cdc',
                                    headers={'X-Internal-Token': 'anything'})
        self.assertEqual(resp.status_code, 503)

    def test_the_tick_needs_no_session(self):
        with mock.patch.dict('os.environ', {'INTERNAL_SCHEDULER_TOKEN': 'shhh'}):
            with mock.patch.object(
                connection_routes.scheduler, 'scheduled_cdc_tick',
                return_value={'started': [], 'skipped': [], 'failed': [], 'due': 0},
            ):
                resp = self.client.post('/internal/scheduler/cdc',
                                        headers={'X-Internal-Token': 'shhh'})
        self.assertEqual(resp.status_code, 200)


class DetailTests(_RouteTestCase):
    def test_detail_carries_what_the_screen_renders(self):
        cid = self.make(ready=True, sp=True)
        run_id = runs.create_run(cid, mode='cdc', trigger_type='scheduled')
        runs.update_run(run_id, 'success')
        self.sign_in()
        body = self.client.get(f'/api/connections/{cid}').get_json()
        self.assertEqual(body['connection']['connection_id'], cid)
        self.assertEqual(body['bootstrap']['warehouse_id'], 'wh-1')
        self.assertEqual(body['latest_run']['state'], 'success')
        self.assertTrue(body['has_service_principal'])
        self.assertEqual(body['next_step'], 'sync')


if __name__ == '__main__':
    unittest.main()
