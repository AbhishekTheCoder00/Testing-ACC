"""
Purpose: Pin the connection_id derivation from FR-05 §7 / FR-03 §11.1, since it is the key
every piece of connection-scoped state hangs off — pipelines, bootstrap, watermarks, runs.
If this hash ever changes, every existing connection silently orphans its state, so the
expected digest is asserted literally rather than recomputed. Traces TC-CONN-09.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')

from backend.services.m2m.connection_identity import (  # noqa: E402
    compute_connection_id,
    normalize_workspace_url,
)

HUB = 'b.7f3a9b2c'
PROJECT = 'b.proj-1'
WORKSPACE = 'https://dbc-abc123.cloud.databricks.com'
CATALOG = 'bronze_acme_west'


class ConnectionIdTests(unittest.TestCase):
    def test_matches_the_spec_formula(self):
        """FR-05 §7 verbatim: sha256 of the pipe-joined quadruple, first 32 hex chars."""
        raw = f"{HUB}|{PROJECT}|{WORKSPACE}|{CATALOG}"
        expected = 'cnx_' + hashlib.sha256(raw.encode()).hexdigest()[:32]
        self.assertEqual(compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG), expected)

    def test_shape(self):
        cid = compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG)
        self.assertTrue(cid.startswith('cnx_'))
        self.assertEqual(len(cid), 36)
        self.assertTrue(all(c in '0123456789abcdef' for c in cid[4:]))

    def test_is_deterministic(self):
        self.assertEqual(
            compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG),
            compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG),
        )

    def test_trailing_slash_on_workspace_is_ignored(self):
        self.assertEqual(
            compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG),
            compute_connection_id(HUB, PROJECT, WORKSPACE + '/', CATALOG),
        )

    def test_multiple_trailing_slashes_and_whitespace_ignored(self):
        self.assertEqual(
            compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG),
            compute_connection_id(
                f'  {HUB} ', f' {PROJECT}', f' {WORKSPACE}///  ', f'{CATALOG} ',
            ),
        )

    def test_each_component_changes_the_hash(self):
        base = compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG)
        variants = [
            compute_connection_id('b.other', PROJECT, WORKSPACE, CATALOG),
            compute_connection_id(HUB, 'b.other', WORKSPACE, CATALOG),
            compute_connection_id(HUB, PROJECT, 'https://dbc-def456.cloud.databricks.com', CATALOG),
            compute_connection_id(HUB, PROJECT, WORKSPACE, 'bronze_other'),
        ]
        for other in variants:
            self.assertNotEqual(base, other)
        self.assertEqual(len(set(variants)), 4)

    def test_catalog_is_case_sensitive(self):
        # Unity Catalog names are case-insensitive for lookup but we hash what was chosen;
        # folding case here would let two spellings collide onto one connection.
        self.assertNotEqual(
            compute_connection_id(HUB, PROJECT, WORKSPACE, 'bronze'),
            compute_connection_id(HUB, PROJECT, WORKSPACE, 'BRONZE'),
        )

    def test_component_order_matters(self):
        self.assertNotEqual(
            compute_connection_id(HUB, PROJECT, WORKSPACE, CATALOG),
            compute_connection_id(PROJECT, HUB, WORKSPACE, CATALOG),
        )

    def test_missing_component_is_rejected(self):
        for args in (
            ('', PROJECT, WORKSPACE, CATALOG),
            (HUB, '', WORKSPACE, CATALOG),
            (HUB, PROJECT, '', CATALOG),
            (HUB, PROJECT, WORKSPACE, '   '),
        ):
            with self.assertRaises(ValueError):
                compute_connection_id(*args)


class NormalizeWorkspaceUrlTests(unittest.TestCase):
    def test_strips_trailing_slashes_and_whitespace(self):
        self.assertEqual(normalize_workspace_url('  https://x.com//  '), 'https://x.com')

    def test_leaves_a_clean_url_alone(self):
        self.assertEqual(normalize_workspace_url(WORKSPACE), WORKSPACE)


if __name__ == '__main__':
    unittest.main()
