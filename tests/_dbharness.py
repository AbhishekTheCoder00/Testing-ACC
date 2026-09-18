"""
Purpose: Repository tests need a real schema but must not touch the developer's connector.db.
This harness points database.DB_PATH at a per-test temp file and runs init_db(), so every
test class gets an isolated, fully-migrated SQLite database. Not named test_*.py on purpose —
unittest discovery must not collect it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key')


class TempDbTestCase(unittest.TestCase):
    """Isolated SQLite state store, rebuilt for every test."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix='repo-test-')
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

        from backend.repositories.state import database

        self.database = database
        patcher = mock.patch.object(
            database, 'DB_PATH', os.path.join(self.tmpdir, 'connector.db'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        env = mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'memory'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('APS_CLIENT_ID', None)

        from backend.secrets import reset_secret_store

        reset_secret_store()
        self.addCleanup(reset_secret_store)

        database.init_db()
