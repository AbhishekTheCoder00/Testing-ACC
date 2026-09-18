
import json
import logging

logger = logging.getLogger(__name__)

##########################################

import json

from datetime import datetime, timezone

from backend.clients import acc_client
from backend.clients.databricks_client import DatabricksClient

from .sync_config import (
    _DC_EXTRACT_ZIP_NAME,
    _DC_EXPORT_SCHEMAS_PREFIX,
    _DC_METADATA_FILE_NAME,
)

#############################################

# ---------------------------------------------------------------------------
# Bulk path — Phase 2 implementations
# ---------------------------------------------------------------------------


def _phase2_in_flask(dbx: DatabricksClient, get_acc_token, account_id: str,
                     job_id: str, vol_dir: str) -> tuple[bytes | None, int]:
    """Legacy Phase 2 — pull each DC signed URL through Flask, upload to UC.

    Kept for one release behind ``ENABLE_NOTEBOOK_DOWNLOAD=false`` so the
    new notebook-based path can be validated in production with a clean
    rollback. Will be deleted after two consecutive successful syncs on
    the notebook path.

    Returns ``(extract_zip_bytes_or_none, file_count)``. The extract zip
    bytes feed Phase 2.5 in-memory so we never re-download or re-read the
    consolidated artifact from the volume.
    """
    files       = acc_client.dc_list_files(get_acc_token, account_id, job_id)
    file_count  = 0
    extract_zip = None
    for file_entry in files:
        file_name  = file_entry['name']
        signed_url = acc_client.dc_get_signed_url(
            get_acc_token, account_id, job_id, file_name,
        )
        data       = acc_client.dc_download_file(signed_url)
        vol_path   = f'{vol_dir}/{file_name}'
        dbx.put_file(vol_path, data, overwrite=True)
        file_count += 1
        logger.info('Uploaded %s (%d bytes)', file_name, len(data))
        if file_name == _DC_EXTRACT_ZIP_NAME:
            extract_zip = data
    return extract_zip, file_count


def _append_schema_and_metadata_manifest_entries(files: list[dict], get_acc_token,
                                                 account_id: str, job_id: str,
                                                 vol_dir: str,
                                                 manifest_files: list[dict]) -> int:
    """Append schema docs + metadata artifact entries to the manifest.

    Data Connector can publish schema artifacts under ``docs/schemas/`` and a
    top-level ``metadata.csv`` file. The bulk_downloader is file-agnostic, so
    all we need is to append these files with signed URLs and target paths.

    Schema docs are normalized under ``{vol_dir}/schemas/{filename}`` so the
    downstream pipeline has a stable location for schema-driven parsing.
    """
    existing_names = {item.get('file_name') for item in manifest_files}
    added = 0

    for file_entry in files:
        file_name = file_entry['name']
        is_schema_doc = file_name.startswith(_DC_EXPORT_SCHEMAS_PREFIX)
        is_metadata = file_name == _DC_METADATA_FILE_NAME
        if not (is_schema_doc or is_metadata):
            continue
        if file_name in existing_names:
            continue

        signed_url = acc_client.dc_get_signed_url(
            get_acc_token, account_id, job_id, file_name,
        )
        if is_schema_doc:
            leaf_name = file_name.rsplit('/', 1)[-1]
            target_volume_path = f'{vol_dir}/schemas/{leaf_name}'
        else:
            target_volume_path = f'{vol_dir}/{_DC_METADATA_FILE_NAME}'

        manifest_files.append({
            'file_name': file_name,
            'signed_url': signed_url,
            'target_volume_path': target_volume_path,
        })
        existing_names.add(file_name)
        added += 1

    return added


def _phase2_via_notebook(dbx: DatabricksClient, get_acc_token, account_id: str,
                         job_id: str, vol_dir: str) -> tuple[str | None, int]:
    """Phase 2 — build manifest for the Databricks sync workflow (no downloads in Flask).

    Returns ``(manifest_path_or_none, manifest_file_count)``.
    """
    files = acc_client.dc_list_files(get_acc_token, account_id, job_id)
    if not files:
        return None, 0

    manifest_files: list[dict] = []

    for file_entry in files:
        file_name = file_entry['name']
        if file_name.startswith(_DC_EXPORT_SCHEMAS_PREFIX) or file_name == _DC_METADATA_FILE_NAME:
            continue
        signed_url = acc_client.dc_get_signed_url(
            get_acc_token, account_id, job_id, file_name,
        )
        manifest_files.append({
            'file_name':          file_name,
            'signed_url':         signed_url,
            'target_volume_path': f'{vol_dir}/{file_name}',
        })

    added = _append_schema_and_metadata_manifest_entries(
        files, get_acc_token, account_id, job_id, vol_dir, manifest_files,
    )
    if added:
        logger.info('Manifest enriched with %d schema/metadata artifact(s)', added)

    if not manifest_files:
        logger.info('No downloadable artifacts in DC listing — empty manifest')
        return None, 0

    manifest = {
        'version':    1,
        'job_id':     job_id,
        'vol_dir':    vol_dir,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'files':      manifest_files,
    }
    manifest_path  = f'{vol_dir}/_manifest.json'
    manifest_bytes = json.dumps(manifest, indent=2).encode('utf-8')
    dbx.put_file(manifest_path, manifest_bytes, overwrite=True)
    logger.info(
        'Manifest written: %s (%d files, job_id=%s, created_at=%s) — '
        'signed URLs minted now; bulk_download should start soon',
        manifest_path,
        len(manifest_files),
        job_id,
        manifest['created_at'],
    )
    return manifest_path, len(manifest_files)

