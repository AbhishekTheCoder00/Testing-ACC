# Databricks notebook source
# acc-connector — Data Connector bulk downloader.
#
# Runs inside the customer's Databricks workspace as a regular Job task.
# Reads a manifest of (signed_url, target_volume_path) tuples written by the
# SaaS control plane, downloads each Data Connector signed URL directly into
# UC Volume, and writes a _SUCCESS sentinel when done. DC signed URLs require
# no Authorization header, so this notebook never sees an ACC token — the
# control plane keeps the long-lived secret.
#
# Triggered as bulk_download / bulk_download_cdc in the combined snapshot
# workflow, or as bulk_download in the CDC-only workflow. Each task gets
# download_role via job base_parameters (snapshot | cdc); run-now passes
# manifest_path and cdc_manifest_path. Serverless often omits taskKey tags,
# so download_role is the reliable selector — not taskKey alone.
# On any uncaught failure the notebook removes the entire vol_dir so a half-
# written CSV cannot poison the downstream AUTO CDC FROM SNAPSHOT pipeline.

import concurrent.futures
import json
import os
import time
from datetime import datetime, timezone

import requests


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------
dbutils.widgets.text('manifest_path', '')
dbutils.widgets.text('cdc_manifest_path', '')
dbutils.widgets.text('download_role', '')


def _job_task_key() -> str:
    try:
        return (
            dbutils.notebook.entry_point.getDbutils()
            .notebook().getContext().tags().get('taskKey').get()
        )
    except Exception:
        return ''


def _resolve_download_role() -> str:
    """Pick snapshot vs CDC manifest. Prefer download_role widget (serverless-safe)."""
    role = dbutils.widgets.get('download_role').strip().lower()
    if role in ('snapshot', 'cdc'):
        return role
    task_key = _job_task_key()
    if task_key == 'bulk_download_cdc':
        return 'cdc'
    return 'snapshot'


_download_role = _resolve_download_role()
_task_key = _job_task_key()
if _download_role == 'cdc':
    MANIFEST_PATH = dbutils.widgets.get('cdc_manifest_path').strip()
    _manifest_widget = 'cdc_manifest_path'
else:
    MANIFEST_PATH = dbutils.widgets.get('manifest_path').strip()
    _manifest_widget = 'manifest_path'

if not MANIFEST_PATH:
    raise ValueError(
        f'{_manifest_widget} widget is empty for download_role={_download_role!r} — '
        'caller must pass it via notebook_params'
    )

# Optional tunable. ThreadPoolExecutor parallelism for the download loop.
# DC signed URLs resolve to S3 — parallel-friendly. Keep modest by default;
# a small classic single-node cluster handles 8 concurrent streams fine.
try:
    DOWNLOAD_PARALLELISM = int(spark.conf.get('acc.download_parallelism', '8'))
except Exception:
    DOWNLOAD_PARALLELISM = 8

# Per-file streaming buffer. 64 KB keeps driver memory bounded regardless of
# CSV size; the file is written chunk-by-chunk straight to the volume.
CHUNK_BYTES = 64 * 1024

# Per-file retry policy. 403/410 → fail fast (signed URL expired); transient
# 5xx and network errors → 3 attempts with exponential backoff.
RETRY_BACKOFFS_SEC = (2, 4, 8)

print(
    f'[bulk_downloader] download_role={_download_role!r} task_key={_task_key!r} '
    f'manifest={MANIFEST_PATH}'
)
print(f'[bulk_downloader] parallelism={DOWNLOAD_PARALLELISM} chunk_bytes={CHUNK_BYTES}')
if _download_role == 'cdc' and '/data_connector/' in MANIFEST_PATH and '/data_connector_cdc/' not in MANIFEST_PATH:
    raise ValueError(
        'download_role=cdc but manifest_path points at data_connector/ — '
        're-bootstrap workflow (download_role base_parameters) and re-run sync'
    )
if _download_role == 'snapshot' and '/data_connector_cdc/' in MANIFEST_PATH:
    raise ValueError(
        'download_role=snapshot but manifest_path points at data_connector_cdc/ — '
        'check workflow base_parameters and notebook_params'
    )


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------
with open(MANIFEST_PATH, 'r') as fh:
    manifest = json.load(fh)

vol_dir = manifest['vol_dir']
files   = manifest.get('files') or []
if not files:
    raise ValueError(f'Manifest at {MANIFEST_PATH} contains no files[]')

_manifest_created = manifest.get('created_at', '')
_manifest_job_id = manifest.get('job_id', '')
print(
    f'[bulk_downloader] vol_dir={vol_dir}  file_count={len(files)}  '
    f'manifest_created_at={_manifest_created!r}  dc_job_id={_manifest_job_id!r}'
)
if _manifest_created:
    print(
        '[bulk_downloader] NOTE: signed URLs were minted at manifest creation — '
        'if bulk_download starts long after that, watch for HTTP 403/410 in logs'
    )


# ---------------------------------------------------------------------------
# Per-file download
# ---------------------------------------------------------------------------
class _FastFailHTTP(RuntimeError):
    """Raised on 403/410 — signed URL has expired or been revoked."""


def _download_one(item: dict) -> tuple[str, int]:
    file_name           = item['file_name']
    signed_url          = item['signed_url']
    target_volume_path  = item['target_volume_path']

    last_exc: Exception | None = None
    for attempt, backoff in enumerate(RETRY_BACKOFFS_SEC, start=1):
        try:
            with requests.get(signed_url, stream=True, timeout=300) as resp:
                if resp.status_code in (403, 410):
                    # Signed URL expired — no point retrying. Fail fast.
                    raise _FastFailHTTP(
                        f'{file_name}: HTTP {resp.status_code} on signed URL '
                        f'(expired or revoked) — re-run sync to mint fresh URLs'
                    )
                resp.raise_for_status()

                total = 0
                with open(target_volume_path, 'wb') as out:
                    for chunk in resp.iter_content(chunk_size=CHUNK_BYTES):
                        if chunk:
                            out.write(chunk)
                            total += len(chunk)
                print(f"[bulk_downloader] OK     {file_name} → {target_volume_path} ({total} bytes, attempt {attempt})")
                return file_name, total

        except _FastFailHTTP:
            raise
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status and 500 <= status < 600:
                last_exc = exc
                print(f"[bulk_downloader] WARN   {file_name}: HTTP {status} (attempt {attempt}); sleeping {backoff}s")
                time.sleep(backoff)
                continue
            raise
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            print(f"[bulk_downloader] WARN   {file_name}: {exc} (attempt {attempt}); sleeping {backoff}s")
            time.sleep(backoff)
            continue

    raise RuntimeError(f'{file_name}: exhausted {len(RETRY_BACKOFFS_SEC)} retries: {last_exc}')


# ---------------------------------------------------------------------------
# Drive the loop with bounded parallelism. On any uncaught failure, remove
# the entire vol_dir so a half-written CSV cannot poison the AUTO CDC FROM
# SNAPSHOT pipeline that reads `{DC_BASE}/*/*/{csv_name}` across runs.
# ---------------------------------------------------------------------------
started_at = time.time()
succeeded  = False
_total_bytes = 0
_progress_every = max(1, min(25, len(files) // 10 or 1))
try:
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_PARALLELISM) as pool:
        futures = [pool.submit(_download_one, item) for item in files]
        for fut in concurrent.futures.as_completed(futures):
            file_name, nbytes = fut.result()  # surfaces the first failure
            completed += 1
            _total_bytes += nbytes
            if completed == 1 or completed == len(files) or completed % _progress_every == 0:
                pct = round(100 * completed / len(files))
                elapsed = time.time() - started_at
                print(
                    f'[bulk_downloader] PROGRESS {completed}/{len(files)} ({pct}%) '
                    f'elapsed={elapsed:.0f}s bytes_so_far={_total_bytes}'
                )

    # _SUCCESS sentinel — observability for Flask + manual debugging.
    sentinel_path = f'{vol_dir}/_SUCCESS'
    with open(sentinel_path, 'w') as fh:
        json.dump({
            'completed_at': datetime.now(timezone.utc).isoformat(),
            'file_count':   completed,
            'duration_sec': round(time.time() - started_at, 1),
        }, fh)
    print(
        f'[bulk_downloader] DONE   {completed}/{len(files)} files in '
        f'{time.time() - started_at:.1f}s total_bytes={_total_bytes}; '
        f'sentinel at {sentinel_path} — snapshot_pipeline/cdc_pipeline may start next'
    )
    succeeded = True

finally:
    if not succeeded:
        # Best-effort cleanup. Use dbutils.fs.rm so the recursive remove works
        # across DBFS-backed and UC Volume paths uniformly.
        try:
            dbutils.fs.rm(vol_dir, recurse=True)
            print(f"[bulk_downloader] CLEAN  removed orphan vol_dir on failure: {vol_dir}")
        except Exception as cleanup_exc:
            # Don't mask the original error with a cleanup failure.
            print(f"[bulk_downloader] CLEAN  orphan cleanup failed (continuing): {cleanup_exc}")
