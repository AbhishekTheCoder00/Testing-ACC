import os
import time
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from .constants import *
from .http_client import (  _get, _post )

import requests

logger = logging.getLogger(__name__)

# Callable[[], str] or Callable[..., str] with optional refresh= kwarg.
TokenGetter = Callable[..., str]

_ACC_REAUTH_MSG = 'ACC session expired — reconnect ACC in the connector.'


def _dc_get(get_token: TokenGetter, url: str, **kwargs):
    """GET with one 401 retry after forcing a fresh token."""
    try:
        return _get(url, get_token(), **kwargs)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 401:
            raise
        logger.info('ACC 401 on DC request — refreshing token and retrying once')
        try:
            return _get(url, get_token(refresh=True), **kwargs)
        except requests.HTTPError as retry_exc:
            if retry_exc.response is not None and retry_exc.response.status_code == 401:
                raise RuntimeError(_ACC_REAUTH_MSG) from retry_exc
            raise
        except TypeError:
            # Getter does not accept refresh= (legacy str wrapper) — fail clearly.
            raise RuntimeError(_ACC_REAUTH_MSG) from exc

# ---------------------------------------------------------------------------
# Data Connector API — bulk async export of all service groups
# ---------------------------------------------------------------------------

def _acc_region() -> str:
    return os.getenv('ACC_REGION', 'US')


def _dc_headers_extra() -> dict:
    return {'region': _acc_region()}

###########################################

def _normalize_service_groups(service_groups: list) -> list:
    """Enforce ACC rule: ``all`` alone OR individual groups, never mixed.

    See POST /requests serviceGroups —
    https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-POST/
    """
    if not service_groups:
        return list(DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS)
    groups = [g.strip() for g in service_groups if g and str(g).strip()]
    if not groups:
        return list(DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS)
    if 'all' in groups:
        if len(groups) != 1:
            raise ValueError(
                "ACC Data Connector serviceGroups: use only ['all'] OR "
                'individual groups, not both (400 Bad service groups).'
            )
        return ['all']
    return groups


def dc_create_request(token: str, account_id: str, project_id: str,
                      start_date: str = None, end_date: str = None,
                      service_groups: list | None = None) -> str:
    """POST /requests — Pipeline A export (snapshot-only groups by default).

    Pass ``DC_FULL_EXPORT_SERVICE_GROUPS`` (``['all']``) explicitly for a full
    standard export. See POST /requests serviceGroups.
    """
    return _dc_create_request(
        token, account_id, project_id,
        service_groups=_normalize_service_groups(
            service_groups if service_groups is not None
            else DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS
        ),
        start_date=start_date,
        end_date=end_date,
    )


def dc_create_cdc_request(token: str, account_id: str, project_id: str,
                          start_date: str = None, end_date: str = None,
                          service_groups: list | None = None) -> str:
    """POST /requests for CDC-beta service groups only.

    ``service_groups`` may narrow the run to a subset of CDC groups; falsy
    falls back to the full ``DC_CDC_SERVICE_GROUPS`` list.
    """
    return _dc_create_request(
        token, account_id, project_id,
        service_groups=_normalize_service_groups(
            service_groups if service_groups is not None else DC_CDC_SERVICE_GROUPS
        ),
        start_date=start_date,
        end_date=end_date,
    )


def _dc_create_request(token: str, account_id: str, project_id: str,
                       service_groups: list, start_date: str = None,
                       end_date: str = None) -> str:
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    # ACC requires startDate and endDate together or neither (400 otherwise).
    if start_date and not end_date:
        end_date = now
    elif end_date and not start_date:
        start_date = end_date
    payload = {
        'projectId':        project_id,
        'isActive':         True,
        'scheduleInterval': 'ONE_TIME',
        'effectiveFrom':    now,
        'serviceGroups':    service_groups,
        'sendEmail':        False,
    }
    if start_date:
        payload['startDate'] = start_date
    if end_date:
        payload['endDate'] = end_date
    url = f'{DC_BASE}/accounts/{account_id}/requests'
    import json as _json
    logger.info('DC create_request payload: %s', _json.dumps(payload, ensure_ascii=True))
    resp = _post(url, token, payload, extra_headers=_dc_headers_extra())
    request_id = resp['id']
    logger.info('DC request created: %s', request_id)
    return request_id


def dc_wait_for_job(get_token: TokenGetter, account_id: str, request_id: str) -> str:
    """Poll /requests/{requestId}/jobs until a job appears. Returns jobId."""
    url   = f'{DC_BASE}/accounts/{account_id}/requests/{request_id}/jobs'
    start = time.time()
    while time.time() - start < DC_JOB_APPEAR_WAIT:
        try:
            resp    = _dc_get(get_token, url, extra_headers=_dc_headers_extra())
            results = resp.get('results', []) if isinstance(resp, dict) else []
            if results:
                job_id = results[0]['id']
                logger.info('DC job queued: %s', job_id)
                return job_id
        except RuntimeError:
            raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code >= 500:
                logger.warning('DC wait_for_job transient error (will retry): %s', exc)
            else:
                raise
        except requests.RequestException as exc:
            logger.warning('DC wait_for_job transient error (will retry): %s', exc)
        elapsed = int(time.time() - start)
        logger.info('Waiting for DC job to appear... (%ds elapsed)', elapsed)
        time.sleep(15)
    raise TimeoutError(f'No job appeared for request {request_id} within {DC_JOB_APPEAR_WAIT}s')


def dc_poll_job(get_token: TokenGetter, account_id: str, job_id: str) -> dict:
    """Poll /jobs/{jobId} until status is complete. Returns the full job object."""
    url   = f'{DC_BASE}/accounts/{account_id}/jobs/{job_id}'
    start = time.time()
    while time.time() - start < DC_JOB_MAX_WAIT:
        try:
            resp   = _dc_get(get_token, url, extra_headers=_dc_headers_extra())
            status = resp.get('status', 'unknown').lower()
            elapsed = int(time.time() - start)
            logger.info('DC job %s status: %s (%ds elapsed)', job_id, status, elapsed)
            if status == 'complete':
                return resp
            if status in ('fail', 'failed', 'error', 'cancelled'):
                raise RuntimeError(f'Data Connector job {job_id} failed with status: {status}')
        except RuntimeError:
            raise
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code >= 500:
                logger.warning('DC poll_job transient error (will retry): %s', exc)
            else:
                raise
        except requests.RequestException as exc:
            logger.warning('DC poll_job transient error (will retry): %s', exc)
        time.sleep(DC_JOB_POLL_INTERVAL)
    raise TimeoutError(f'DC job {job_id} did not complete within {DC_JOB_MAX_WAIT}s')


def dc_list_files(get_token: TokenGetter, account_id: str, job_id: str) -> list:
    """GET /jobs/{jobId}/data-listing — returns plain list of file objects."""
    url   = f'{DC_BASE}/accounts/{account_id}/jobs/{job_id}/data-listing'
    resp  = _dc_get(get_token, url, extra_headers=_dc_headers_extra())
    files = resp if isinstance(resp, list) else resp.get('results', [])
    logger.info('DC job %s: %d files available', job_id, len(files))
    return files


def dc_get_signed_url(get_token: TokenGetter, account_id: str, job_id: str,
                      file_name: str) -> str:
    """GET /jobs/{jobId}/data/{name} — returns pre-signed download URL."""
    url       = f'{DC_BASE}/accounts/{account_id}/jobs/{job_id}/data/{file_name}'
    resp      = _dc_get(get_token, url, extra_headers=_dc_headers_extra())
    signed_url = resp.get('signedUrl') or resp.get('url') or resp.get('downloadUrl')
    if not signed_url:
        raise ValueError(f'No signed URL in response for {file_name}: {resp}')
    return signed_url


def dc_download_file(signed_url: str) -> bytes:
    """Download a pre-signed URL (no auth needed). Returns raw bytes."""
    r = requests.get(signed_url, stream=True, timeout=300)
    r.raise_for_status()
    chunks = []
    for chunk in r.iter_content(chunk_size=65536):
        chunks.append(chunk)
    return b''.join(chunks)


def dc_list_requests(token: str, account_id: str, project_id: str = None) -> list:
    """GET /requests — list all export requests for the account.

    Optionally filtered to a specific project (client-side if the API does not
    support the query param).
    """
    url  = f'{DC_BASE}/accounts/{account_id}/requests'
    params = {}
    if project_id:
        params['projectId'] = project_id
    resp = _get(url, token, params=params or None, extra_headers=_dc_headers_extra())
    items = resp.get('results', resp.get('data', [])) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
    # Client-side project filter as fallback
    if project_id and items:
        items = [r for r in items if r.get('projectId') == project_id]
    return items


def dc_delete_request(token: str, account_id: str, request_id: str) -> None:
    """DELETE /requests/{requestId} — cancel/delete a Data Connector request."""
    url     = f'{DC_BASE}/accounts/{account_id}/requests/{request_id}'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    headers.update(_dc_headers_extra())
    r = requests.delete(url, headers=headers, timeout=30)
    if not r.ok:
        body = r.text[:500]
        logger.error('DC delete request %s failed %s — %s', request_id, r.status_code, body)
        raise requests.HTTPError(f'{r.status_code} {r.reason} — {body}', response=r)
    logger.info('DC request %s deleted', request_id)
