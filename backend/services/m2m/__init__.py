"""
Purpose: The multi-tenant, headless M2M path (FR-01…FR-05). Everything here is keyed by
hub_id (tenant) or connection_id, never by a logged-in user, so scheduled sync runs with no
session. connection_runner.py is this package's coordinator and is the one module allowed to
call its siblings — the same exception services/sync/ has. See ADR.md (Phase 1).
"""
