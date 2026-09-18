"""
sync_service.py — Orchestrates sync runs.

Two paths (INGESTION_FINALIZED.md):
  snapshot — DC Standard → data_connector/ → AUTO CDC FROM SNAPSHOT
  cdc      — DC CDC beta → data_connector_cdc/ → AUTO CDC

No per-entity REST ingest. Zerobus diagnostics only (ENABLE_ZEROBUS).

No ACC data is ever written to the connector's disk or database.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

from .sync.sync_config import *
from .sync.download_service import *
from .sync.schema_service import *
from .sync.registry_service import *
from .sync.sync_orchestrator import run_sync, run_cdc_sync, _run_dc_export

__all__ = [
    "run_sync",
    "run_cdc_sync",
    "_run_dc_export",
]
# ---------------------------------------------------------------------------
# Public dispatcher — used by the Flask routes and the scheduler
# ---------------------------------------------------------------------------
