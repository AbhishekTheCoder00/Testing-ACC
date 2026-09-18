"""
Purpose: One connection is one installed pipeline — (hub, ACC project, Databricks workspace,
catalog) — and its id is the key for pipelines, bootstrap state, watermarks and run history.
This module is the single place that derives it, using the sha256 formula from FR-05 §7, so
the value can never drift between routes and services and orphan a connection's state.
"""

from __future__ import annotations

import hashlib

CONNECTION_ID_PREFIX = 'cnx_'
_DIGEST_CHARS = 32


def normalize_workspace_url(workspace_url: str) -> str:
    """Trim whitespace and trailing slashes so one workspace hashes to one value."""
    return (workspace_url or '').strip().rstrip('/')


def _require(name: str, value: str) -> str:
    cleaned = (value or '').strip()
    if not cleaned:
        raise ValueError(f'{name} is required to compute a connection_id')
    return cleaned


def compute_connection_id(
    hub_id: str,
    project_id: str,
    workspace_url: str,
    catalog: str,
) -> str:
    """Deterministic connection id — FR-05 §7.

    Every component is required: a connection is only meaningful as the full quadruple, and
    defaulting any part would let two different targets collapse onto one pipeline.
    """
    hub = _require('hub_id', hub_id)
    project = _require('project_id', project_id)
    workspace = normalize_workspace_url(_require('workspace_url', workspace_url))
    catalog_name = _require('catalog', catalog)

    raw = f'{hub}|{project}|{workspace}|{catalog_name}'
    digest = hashlib.sha256(raw.encode()).hexdigest()[:_DIGEST_CHARS]
    return f'{CONNECTION_ID_PREFIX}{digest}'
