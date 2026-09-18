import os
import time
import logging
from .constants import *

from backend.clients.acc.constants import APS_BASE_AUTH , APS_BASE_DM , APS_BASE_PROFILE , DC_BASE , DC_ALL_SERVICE_GROUPS , DC_CDC_SERVICE_GROUPS , DC_JOB_POLL_INTERVAL , DC_JOB_MAX_WAIT , DC_JOB_APPEAR_WAIT , TOKEN_REFRESH_BUFFER_SEC , MAX_RETRIES , RETRY_DELAYS

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal HTTP helper with retry + exponential backoff
# ---------------------------------------------------------------------------

def _get_all_pages(base_url: str, token: str) -> list:
    """Follow JSON:API pagination links and collect all 'data' items."""
    results = []
    url = base_url
    while url:
        resp = _get(url, token)
        results.extend(resp.get('data', []))
        next_href = resp.get('links', {}).get('next', {})
        url = next_href.get('href') if isinstance(next_href, dict) else None
    return results


def _get(url: str, token: str, params: dict = None, extra_headers: dict = None):
    headers = {'Authorization': f'Bearer {token}'}
    if extra_headers:
        headers.update(extra_headers)
    last_exc = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 429:
                body = r.text[:1000]
                last_exc = requests.HTTPError(f'429 {r.reason} — {body}', response=r)
                retry_after = min(int(r.headers.get('Retry-After', delay)), 60)
                logger.warning('ACC rate limit hit, waiting %ss — body: %s', retry_after, body)
                time.sleep(retry_after)
                continue
            if not r.ok:
                body = r.text[:1000]
                logger.error('ACC API error %s for %s — body: %s', r.status_code, url, body)
                err = requests.HTTPError(f'{r.status_code} {r.reason} — {body}', response=r)
                if r.status_code < 500:
                    raise err   # 4xx: client error, no point retrying
                last_exc = err  # 5xx: transient, fall through to retry
            else:
                return r.json()
        except requests.HTTPError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < len(RETRY_DELAYS) - 1:
            logger.warning('ACC request failed (attempt %d/%d) — retrying in %ds',
                           attempt + 1, len(RETRY_DELAYS), delay)
            time.sleep(delay)
    raise last_exc


def _post(url: str, token: str, json_body: dict = None, extra_headers: dict = None) -> dict:
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    last_exc = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            logger.info('POST %s', url)
            r = requests.post(url, headers=headers, json=json_body, timeout=60)
            if r.status_code == 429:
                body = r.text[:1000]
                last_exc = requests.HTTPError(f'429 {r.reason} — {body}', response=r)
                retry_after = min(int(r.headers.get('Retry-After', delay)), 60)
                logger.warning('ACC rate limit hit, waiting %ss — body: %s', retry_after, body)
                time.sleep(retry_after)
                continue
            if not r.ok:
                body = r.text[:1000]
                logger.error('ACC API error %s for %s — body: %s', r.status_code, url, body)
                err = requests.HTTPError(f'{r.status_code} {r.reason} — {body}', response=r)
                if r.status_code < 500:
                    raise err   # 4xx: fail fast
                last_exc = err  # 5xx: retry
            else:
                return r.json()
        except requests.HTTPError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < len(RETRY_DELAYS) - 1:
            logger.warning('ACC request failed (attempt %d/%d) — retrying in %ds',
                           attempt + 1, len(RETRY_DELAYS), delay)
            time.sleep(delay)
    raise last_exc

