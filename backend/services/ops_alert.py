"""
Purpose: Tells the ISV operators when SSA provisioning capacity is running out, because the
customer cannot see or fix that themselves — enterprise 11 simply cannot onboard until someone
raises the APS quota or registers another Client ID. Structured log lines plus an optional
webhook; no email client and no new dependency, since the operational need is a signal, not a
delivery guarantee.
"""

from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

# Warn once per app per process. A tick that provisions several hubs should not emit the
# same warning five times, and the signal is "someone go look", not a metric.
_sent: set[str] = set()
_lock = threading.Lock()


def _once(key: str) -> bool:
    with _lock:
        if key in _sent:
            return False
        _sent.add(key)
        return True


def reset() -> None:
    """Clear the once-per-process suppression. For tests."""
    with _lock:
        _sent.clear()


def _post_webhook(payload: dict) -> None:
    url = (os.getenv('OPS_ALERT_WEBHOOK_URL') or '').strip()
    if not url:
        return
    try:
        import requests

        requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # an alert that fails must never break provisioning
        logger.warning('ops alert webhook failed: %s', exc)


def send_ssa_quota_warning(app_ref: str, client_id: str, count: int, max_robots: int) -> None:
    """Approaching the service-account ceiling — act before a customer is blocked."""
    if not _once(f'quota-warning:{app_ref}:{count}'):
        return
    message = (
        f'SSA capacity warning: APS app {app_ref} is at {count}/{max_robots} service '
        f'accounts. Raise the quota via ssa-requests@autodesk.com or register another '
        f'Client ID before the next enterprise onboards.'
    )
    logger.error('[ops-alert] %s', message)
    _post_webhook({
        'kind': 'ssa_quota_warning',
        'app_ref': app_ref,
        'client_id': client_id,
        'robot_count': count,
        'max_robots': max_robots,
        'message': message,
    })


def send_ssa_capacity_exhausted(counts: dict[str, int]) -> None:
    """No capacity left anywhere — onboarding is blocked until ops intervenes."""
    if not _once('capacity-exhausted:' + json.dumps(counts, sort_keys=True)):
        return
    message = (
        'SSA capacity exhausted: every active APS app is at its service-account limit '
        f'({counts}). A new hub cannot be provisioned until the quota is raised or a new '
        'Client ID is registered in aps_apps.'
    )
    logger.error('[ops-alert] %s', message)
    _post_webhook({
        'kind': 'ssa_capacity_exhausted',
        'counts': counts,
        'message': message,
    })
