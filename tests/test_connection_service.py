"""
Purpose: The connection service is what makes setup re-runnable — the same (hub, project,
workspace, catalog) must resume, never duplicate, and a catalog already driven by anyone must
be refused by name. These tests also pin the D-4/D-18 gate: scheduled CDC needs both a
Databricks service principal and a live robot-on-project probe, and the refusal must say which
one is missing. Traces TC-CONN-01…13, TC-CONC-03, BR-11.
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
    catalog_claim_repository as claims,
    connection_repository as conns,
    tenant_repository as tenants,
)
from backend.services.m2m import connection_service as svc  # noqa: E402
from backend.services.m2m.connection_identity import compute_connection_id  # noqa: E402
from backend.services.m2m.provisioning_probe import ProbeResult  # noqa: E402

HUB = 'b.hub1'
WS = 'https://dbc-abc123.cloud.databricks.com'


def _passing_probe(*_a, **_kw):
    return ProbeResult(True, 'ok', 'Service account verified on this project.')


class _ServiceTestCase(TempDbTestCase):
    def setUp(self):
        super().setUp()
        apps.insert('app_a', 'AAA', 'ref/a')
        tenants.ensure_tenant(HUB, 'app_a')
        tenants.ensure_tenant('b.hub2', 'app_a')

    def create(self, project='p1', catalog='c1', hub=HUB, workspace=WS, name=None):
        return svc.create_or_resume(
            hub_id=hub,
            project_id=project,
            dbx_workspace_url=workspace,
            catalog=catalog,
            project_name=name,
        )


class CreateOrResumeTests(_ServiceTestCase):
    def test_creates_with_the_deterministic_id(self):
        """TC-CONN-01 — the id is the hash of the quadruple, not a surrogate key."""
        result = self.create()
        self.assertFalse(result.resumed)
        self.assertEqual(
            result.connection['connection_id'],
            compute_connection_id(HUB, 'p1', WS, 'c1'),
        )
        self.assertEqual(
            result.connection['onboarding_status'], 'pending_custom_integration',
        )

    def test_same_quadruple_resumes_instead_of_duplicating(self):
        """TC-CONN-02 — re-running setup on the same target must not make a second row."""
        first = self.create()
        second = self.create()
        self.assertTrue(second.resumed)
        self.assertEqual(
            second.connection['connection_id'], first.connection['connection_id'],
        )
        self.assertEqual(len(conns.list_for_hub(HUB)), 1)

    def test_a_trailing_slash_still_resumes(self):
        first = self.create()
        second = self.create(workspace=WS + '/')
        self.assertTrue(second.resumed)
        self.assertEqual(
            second.connection['connection_id'], first.connection['connection_id'],
        )

    def test_resume_preserves_progress(self):
        """TC-CONN-11 — resuming must not reset the status back to step one."""
        first = self.create()
        cid = first.connection['connection_id']
        conns.update_status(cid, 'pending_bootstrap')
        self.assertEqual(
            self.create().connection['onboarding_status'], 'pending_bootstrap',
        )

    def test_a_different_project_makes_a_new_connection(self):
        """TC-CONN-03 — same hub and target, different project → separate pipeline."""
        first = self.create(project='p1')
        second = self.create(project='p2', catalog='c2')
        self.assertNotEqual(
            first.connection['connection_id'], second.connection['connection_id'],
        )
        self.assertEqual(len(conns.list_for_hub(HUB)), 2)

    def test_a_different_catalog_makes_a_new_connection(self):
        first = self.create(catalog='c1')
        second = self.create(catalog='c2')
        self.assertNotEqual(
            first.connection['connection_id'], second.connection['connection_id'],
        )

    def test_project_name_backfills_on_resume(self):
        self.create(name=None)
        resumed = self.create(name='Riverside Tower')
        self.assertEqual(resumed.connection['project_name'], 'Riverside Tower')

    def test_missing_component_is_rejected(self):
        with self.assertRaises(ValueError):
            self.create(catalog='')


class CatalogExclusivityTests(_ServiceTestCase):
    def test_creating_claims_the_catalog_for_the_connection(self):
        """BR-11 — the claim owner is the connection, so it survives the admin leaving."""
        result = self.create()
        claim = claims.get_active_claim(
            claims.normalize_workspace_key(WS), 'c1',
        )
        self.assertEqual(
            claim['owner_user_id'], f'cnx:{result.connection["connection_id"]}',
        )

    def test_a_catalog_taken_by_another_connection_is_refused_by_name(self):
        """TC-CONN-06 — one catalog, one connection, even across hubs."""
        first = self.create()
        with self.assertRaises(svc.CatalogAlreadyConnected) as caught:
            self.create(hub='b.hub2', project='p9')
        self.assertEqual(caught.exception.catalog, 'c1')
        self.assertIn(first.connection['connection_id'], caught.exception.owner)
        self.assertIsNone(conns.get_by_parts('b.hub2', 'p9', WS, 'c1'))

    def test_a_catalog_claimed_by_a_u2m_user_is_refused(self):
        """Cross-path exclusivity — U2M and M2M cannot both provision one catalog."""
        claims.claim_catalog(claims.normalize_workspace_key(WS), 'c1', 'user-42')
        with self.assertRaises(svc.CatalogAlreadyConnected) as caught:
            self.create()
        self.assertEqual(caught.exception.owner, 'user-42')
        self.assertIsNone(conns.get_by_parts(HUB, 'p1', WS, 'c1'))

    def test_resume_does_not_disturb_the_claim(self):
        first = self.create()
        claim_before = claims.get_active_claim(claims.normalize_workspace_key(WS), 'c1')
        self.create()
        claim_after = claims.get_active_claim(claims.normalize_workspace_key(WS), 'c1')
        self.assertEqual(claim_before['claim_id'], claim_after['claim_id'])
        self.assertEqual(
            claim_after['owner_user_id'], f'cnx:{first.connection["connection_id"]}',
        )

    def test_two_catalogs_in_one_workspace_coexist(self):
        """The per-owner "one catalog at a time" rule must not apply across connections."""
        self.create(catalog='c1')
        self.create(project='p2', catalog='c2')
        key = claims.normalize_workspace_key(WS)
        self.assertIsNotNone(claims.get_active_claim(key, 'c1'))
        self.assertIsNotNone(claims.get_active_claim(key, 'c2'))


class NextStepTests(_ServiceTestCase):
    def test_every_status_maps_to_a_wizard_step(self):
        """TC-CONN-11 — the wizard resumes where the customer left off."""
        for status in conns.CONNECTION_STATUSES:
            self.assertIn(svc.next_step({'onboarding_status': status}), svc.WIZARD_STEPS)

    def test_ready_lands_on_the_last_step(self):
        """FR-06 §4.8 — step 5 "Sync Data" is where a finished connection resumes."""
        self.assertEqual(svc.next_step({'onboarding_status': 'ready'}), 'sync')
        self.assertEqual(svc.WIZARD_STEPS[-1], 'sync')

    def test_an_unknown_status_falls_back_to_the_first_step(self):
        self.assertEqual(svc.next_step({'onboarding_status': 'nonsense'}), 'whitelist')


class MarkReadyTests(_ServiceTestCase):
    def test_first_ready_connection_activates_the_tenant(self):
        """FR-03 §18 Q2 — ssa_active means "a connection actually works", not "robot exists"."""
        result = self.create()
        tenants.update_status(HUB, 'whitelist_verified')
        svc.mark_ready(result.connection['connection_id'])
        self.assertEqual(conns.get(result.connection['connection_id'])['onboarding_status'],
                         'ready')
        self.assertEqual(tenants.get(HUB)['onboarding_status'], 'ssa_active')

    def test_marking_ready_never_moves_a_tenant_backwards(self):
        result = self.create()
        tenants.update_status(HUB, 'ssa_limit_reached')
        svc.mark_ready(result.connection['connection_id'])
        self.assertEqual(tenants.get(HUB)['onboarding_status'], 'ssa_limit_reached')


class EnableCdcTests(_ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.create().connection
        self.cid = self.connection['connection_id']
        conns.update_status(self.cid, 'ready')

    def add_sp(self):
        conns.save_dbx_credentials(
            self.cid, workspace_url=WS, client_id='sp-1',
            client_secret_ref='ref/sp-1', validated_at=1.0,
        )

    def test_refuses_without_a_service_principal_and_names_it(self):
        """D-4 — headless sync may never fall back to a human's Databricks token."""
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               side_effect=AssertionError('must not probe')):
            with self.assertRaises(svc.SchedulingBlocked) as caught:
                svc.enable_cdc(self.cid)
        self.assertEqual(caught.exception.reason, 'missing_service_principal')
        self.assertIn('service principal', caught.exception.remediation.lower())
        self.assertFalse(conns.get(self.cid)['cdc_enabled'])

    def test_refuses_when_the_robot_is_not_on_the_project_and_names_the_robot(self):
        """D-18 — the toggle names the exact missing link, never a generic error."""
        self.add_sp()
        failure = ProbeResult(
            False, 'robot_not_on_project', 'The service account cannot see this project.',
            remediation='In ACC, invite forma-dbx-1@example.com to this project.',
        )
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               return_value=failure):
            with self.assertRaises(svc.SchedulingBlocked) as caught:
                svc.enable_cdc(self.cid)
        self.assertEqual(caught.exception.reason, 'robot_not_on_project')
        self.assertIn('forma-dbx-1@example.com', caught.exception.remediation)
        self.assertFalse(conns.get(self.cid)['cdc_enabled'])

    def test_enables_with_both_a_service_principal_and_a_passing_probe(self):
        self.add_sp()
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               side_effect=_passing_probe):
            svc.enable_cdc(self.cid, now=1_000.0)
        row = conns.get(self.cid)
        self.assertTrue(row['cdc_enabled'])
        self.assertEqual(row['auto_cdc_next_run_at'], 1_000.0 + svc.CDC_INTERVAL_SEC)

    def test_refuses_before_the_connection_is_ready(self):
        conns.update_status(self.cid, 'pending_bootstrap')
        self.add_sp()
        with self.assertRaises(svc.SchedulingBlocked) as caught:
            svc.enable_cdc(self.cid)
        self.assertEqual(caught.exception.reason, 'not_bootstrapped')

    def test_probe_is_run_for_this_connections_project(self):
        """D-17 — the answer lives inside Autodesk, so it is asked live, per project."""
        self.add_sp()
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               side_effect=_passing_probe) as probe:
            svc.enable_cdc(self.cid)
        probe.assert_called_once_with(HUB, 'p1')

    def test_disable_clears_the_next_run(self):
        self.add_sp()
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               side_effect=_passing_probe):
            svc.enable_cdc(self.cid)
        svc.disable_cdc(self.cid)
        row = conns.get(self.cid)
        self.assertFalse(row['cdc_enabled'])
        self.assertIsNone(row['auto_cdc_next_run_at'])

    def test_disable_needs_no_probe(self):
        with mock.patch.object(svc.provisioning_probe, 'verify_robot_on_project',
                               side_effect=AssertionError('must not probe')):
            svc.disable_cdc(self.cid)

    def test_unknown_connection_is_rejected(self):
        with self.assertRaises(svc.SchedulingBlocked):
            svc.enable_cdc('cnx_nope')


if __name__ == '__main__':
    unittest.main()
