
import time
from .database import _conn
# ---------------------------------------------------------------------------
# Bootstrap state
# ---------------------------------------------------------------------------

def _normalize_bootstrap_row(row: dict | None) -> dict | None:
    """Expose ``snapshot_pipeline_id``; fall back to legacy ``bronze_pipeline_id``."""
    if not row:
        return None
    out = dict(row)
    if not out.get('snapshot_pipeline_id') and out.get('bronze_pipeline_id'):
        out['snapshot_pipeline_id'] = out['bronze_pipeline_id']
    return out


def save_bootstrap_state(user_id: str,
                          notebook_folder: str, volume_path: str,
                          compute_type: str, warehouse_id: str = None,
                          catalog_name: str = None,
                          snapshot_pipeline_id: str = None,
                          cdc_pipeline_id: str = None,
                          bronze_job_id: int = None,
                          silver_job_id: int = None,
                          download_job_id: int = None,
                          snapshot_workflow_id: int = None,
                          cdc_workflow_id: int = None,
                          catalog_claim_id: int = None) -> None:
    """Persist the bootstrap result.

    ``snapshot_pipeline_id`` is the AUTO CDC FROM SNAPSHOT pipeline created in
    Step 10. ``bronze_job_id`` and ``silver_job_id`` are retained as nullable
    columns for one release so a rollback can re-populate them; new
    bootstraps leave both NULL (Silver layer was removed in this release).
    ``download_job_id`` is the Databricks Job created in Step 9b that runs
    ``bulk_downloader.py`` in the customer's workspace; populated only when
    that step is enabled.
    """
    with _conn() as con:
        con.execute('''
            INSERT INTO bootstrap_state
                (user_id, bronze_job_id, silver_job_id, notebook_folder,
                 volume_path, compute_type, warehouse_id, bootstrapped_at,
                 catalog_name, bronze_pipeline_id, snapshot_pipeline_id,
                 download_job_id, cdc_pipeline_id, snapshot_workflow_id,
                 cdc_workflow_id, catalog_claim_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                bronze_job_id          = excluded.bronze_job_id,
                silver_job_id          = excluded.silver_job_id,
                notebook_folder        = excluded.notebook_folder,
                volume_path            = excluded.volume_path,
                compute_type           = excluded.compute_type,
                warehouse_id           = excluded.warehouse_id,
                bootstrapped_at        = excluded.bootstrapped_at,
                catalog_name           = excluded.catalog_name,
                bronze_pipeline_id     = excluded.snapshot_pipeline_id,
                snapshot_pipeline_id   = excluded.snapshot_pipeline_id,
                download_job_id        = excluded.download_job_id,
                cdc_pipeline_id        = excluded.cdc_pipeline_id,
                snapshot_workflow_id   = excluded.snapshot_workflow_id,
                cdc_workflow_id        = excluded.cdc_workflow_id,
                catalog_claim_id       = excluded.catalog_claim_id
        ''', (user_id, bronze_job_id, silver_job_id, notebook_folder,
              volume_path, compute_type, warehouse_id, time.time(),
              catalog_name, snapshot_pipeline_id, snapshot_pipeline_id,
              download_job_id, cdc_pipeline_id, snapshot_workflow_id,
              cdc_workflow_id, catalog_claim_id))


def get_bootstrap_state(user_id: str) -> dict | None:
    with _conn() as con:
        row = con.execute('SELECT * FROM bootstrap_state WHERE user_id = ?', (user_id,)).fetchone()
    return _normalize_bootstrap_row(dict(row) if row else None)
