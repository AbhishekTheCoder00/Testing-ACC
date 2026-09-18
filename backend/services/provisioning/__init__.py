"""
Purpose: Workspace provisioning logic shared by both auth paths. Nothing in here knows about
users, hubs, connections or the database — callers pass a DatabricksClient and a progress
callback and get back the artefact ids. That is what lets the U2M and M2M bootstrap drivers
run the same sequence instead of keeping two copies of it. See ADR.md (Phase 1).
"""
