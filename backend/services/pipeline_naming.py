"""pipeline_naming.py — per-catalog names for every provisioned Databricks artifact.

Every artifact the connector creates used to carry a fixed, workspace-global
name. ``get_or_create_pipeline`` resolves by name, and the workflow builders
run with ``replace=True`` (delete-then-recreate by name), so two connector
users sharing one Databricks workspace collided: the second bootstrap
repointed — or outright deleted — the first user's artifacts.

Names are now derived from the Unity Catalog they serve. The catalog is the
natural isolation boundary: one catalog is provisioned by exactly one user
(enforced in ``catalog_claim_repository``), so catalog-scoped names are
unique per owner without leaking user identity into the workspace.

Pure module — no I/O, no Databricks calls, no DB access.
"""
import hashlib

_PREFIX_ACC   = 'CCTech_ACCConnector'
_PREFIX_FORMA = 'CCTech_FormaConnector'

# UC catalog names run up to 128 chars, which would push artifact names past
# what the Jobs/Pipelines APIs accept. Long catalogs are truncated and given a
# hash suffix: the slug must stay *deterministic* because artifacts are looked
# up by name — an unstable slug would orphan the previous pipeline on every run.
_MAX_SLUG = 40
_HASH_LEN = 8


def catalog_slug(catalog_name: str) -> str:
    """Return the name fragment identifying ``catalog_name`` in an artifact name."""
    name = (catalog_name or '').strip()
    if not name:
        raise ValueError('catalog_name is required to derive artifact names')
    if len(name) <= _MAX_SLUG:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LEN]
    return f'{name[:_MAX_SLUG]}_{digest}'


def snapshot_pipeline_name(catalog_name: str) -> str:
    return f'{_PREFIX_ACC}_{catalog_slug(catalog_name)}_PipelineA_Snapshot'


def cdc_pipeline_name(catalog_name: str) -> str:
    return f'{_PREFIX_ACC}_{catalog_slug(catalog_name)}_PipelineB_DeltaCDC'


def snapshot_workflow_name(catalog_name: str) -> str:
    return f'{_PREFIX_ACC}_{catalog_slug(catalog_name)}_Snapshot_Sync'


def cdc_workflow_name(catalog_name: str) -> str:
    return f'{_PREFIX_ACC}_{catalog_slug(catalog_name)}_CDC_Sync'


def download_job_name(catalog_name: str) -> str:
    return f'{_PREFIX_FORMA}_{catalog_slug(catalog_name)}_DCDownloader'


def artifact_names(catalog_name: str) -> dict:
    """All five artifact names for one catalog, keyed by role."""
    return {
        'snapshot_pipeline': snapshot_pipeline_name(catalog_name),
        'cdc_pipeline':      cdc_pipeline_name(catalog_name),
        'snapshot_workflow': snapshot_workflow_name(catalog_name),
        'cdc_workflow':      cdc_workflow_name(catalog_name),
        'download_job':      download_job_name(catalog_name),
    }
