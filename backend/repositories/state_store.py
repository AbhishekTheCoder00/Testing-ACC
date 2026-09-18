"""
state_store.py — SQLite-backed persistence for the ACC Connector.

Tables:
  acc_tokens       — encrypted ACC OAuth tokens per user
  acc_config       — selected hub / project / folder per user
  dbx_credentials  — encrypted Databricks PAT + workspace URL per user
  dbx_tokens       — encrypted Databricks OAuth tokens per user
  bootstrap_state  — job IDs, notebook paths, compute config after bootstrap
  watermarks       — last successful sync timestamp per user/project/data_type
  sync_runs        — state machine record for every sync run

The Phase 1 multi-tenant tables (tenants, connections, …) are reached through the
namespaced modules re-exported at the bottom of this file, not through ``import *``
— their APIs use short verbs (``get``, ``insert``, ``delete``) that would collide.

Legacy artifacts that remain in the DB schema but no live code path touches:
  ``bootstrap_state.bronze_job_id`` / ``silver_job_id`` — pre-pipeline Jobs era
  ``sync_runs.silver_run_id``                          — Silver layer was removed
  An older ``sync_mode`` table on already-bootstrapped DBs — its writers and
  readers were deleted along with the realtime ingestion path.
"""
import sqlite3
import os
import time
from cryptography.fernet import Fernet

"""
Facade for repository layer.

Existing code can continue importing:

from backend.repositories import state_store as db
"""

from .state.database import *
from .state.encryption import *
from .state.token_repository import *
from .state.config_repository import *
from .state.bootstrap_repository import *
from .state.catalog_claim_repository import *
from .state.catalog_claim_repository import CatalogLockedError
from .state.watermark_repository import *
from .state.sync_repository import *
from .state.user_repository import *
from .state.secret_repository import *

# ---------------------------------------------------------------------------
# Phase 1 — multi-tenant M2M repositories.
#
# Re-exported as modules, deliberately NOT star-imported: all five expose short verbs
# (get / insert / delete / update_status) that would shadow each other and the U2M
# helpers above. Use either
#     from backend.repositories import state_store as db;  db.tenant_repository.get(hub)
# or the direct module import
#     from backend.repositories.state import tenant_repository as tenants
# ---------------------------------------------------------------------------

from .state import aps_app_repository  # noqa: F401
from .state import connection_repository  # noqa: F401
from .state import connection_sync_repository  # noqa: F401
from .state import ssa_repository  # noqa: F401
from .state import tenant_repository  # noqa: F401
