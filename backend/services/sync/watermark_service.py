"""Incremental DC export windows — anchor on last *successful* sync, not failed runs."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from backend.repositories import state_store as db

from .sync_config import (
    DC_CDC_WATERMARK_KEY,
    DC_WATERMARK_KEY,
    MANUAL_FULL_WATERMARK_KEY,
)

logger = logging.getLogger(__name__)


def successful_sync_modes_for_watermark(watermark_key: str) -> frozenset[str]:
    """Map a watermark key to sync run ``record_counts.mode`` values that advance it."""
    if watermark_key == DC_CDC_WATERMARK_KEY:
        return frozenset({'cdc', 'snapshot'})
    if watermark_key == DC_WATERMARK_KEY:
        return frozenset({'snapshot'})
    return frozenset()


def parse_sync_run_mode(record_counts_raw: str | None) -> str | None:
    if not record_counts_raw:
        return None
    try:
        doc = json.loads(record_counts_raw)
    except (TypeError, json.JSONDecodeError):
        return None
    mode = doc.get('mode')
    return mode if isinstance(mode, str) else None


def pick_successful_sync_timestamp(
    runs: list[dict],
    watermark_key: str,
) -> float | None:
    """Return ``updated_at`` of the newest complete run that advanced ``watermark_key``."""
    acceptable = successful_sync_modes_for_watermark(watermark_key)
    for row in runs:
        if row.get('state') != 'complete':
            continue
        mode = parse_sync_run_mode(row.get('record_counts'))
        if mode in acceptable:
            return float(row['updated_at'])
        if mode is None and watermark_key in (row.get('data_types') or '').split(','):
            return float(row['updated_at'])
    return None


def iso_window_from_timestamp(ts: float) -> tuple[str, str]:
    start_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.000Z',
    )
    end_date = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return start_date, end_date


def _format_anchor(ts: float | None) -> str:
    if ts is None:
        return 'FULL_EXPORT'
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def resolve_incremental_window(
    user_id: str,
    project_id: str,
    watermark_key: str,
) -> tuple[str | None, str | None]:
    """DC export window from last successful sync; legacy watermark table as fallback."""
    successful_ts = db.get_last_successful_sync_timestamp(user_id, project_id, watermark_key)
    source = 'successful_run'
    ts = successful_ts
    if ts is None:
        ts = db.get_watermark(user_id, project_id, watermark_key)
        source = 'legacy_watermark' if ts is not None else 'none'

    latest = db.get_latest_sync_for_project(user_id, project_id)
    if (
        latest
        and latest.get('state') != 'complete'
        and successful_ts is not None
    ):
        logger.info(
            '[sync-window] latest run_id=%s state=%s ignored — anchor uses last '
            'successful sync, not failed/in-progress run',
            latest.get('run_id'),
            latest.get('state'),
        )

    logger.info(
        '[sync-window] user=%s project=%s key=%s source=%s anchor=%s',
        user_id,
        project_id,
        watermark_key,
        source,
        _format_anchor(ts),
    )
    if ts is None:
        return None, None
    start_date, end_date = iso_window_from_timestamp(ts)
    logger.info(
        '[sync-window] key=%s incremental DC window %s → %s',
        watermark_key,
        start_date,
        end_date,
    )
    return start_date, end_date


def watermark_keys_for(mode: str, trigger_type: str) -> list[str]:
    """Which watermark keys a successful run of this mode advances.

    The rule is the same on both auth paths — only the table the keys are written to differs —
    so it lives here once and both drivers ask for it (see connection_runner).
    """
    if mode == 'snapshot':
        keys = [DC_WATERMARK_KEY, DC_CDC_WATERMARK_KEY]
        if trigger_type == 'manual':
            keys.append(MANUAL_FULL_WATERMARK_KEY)
        return keys
    if mode == 'cdc':
        return [DC_CDC_WATERMARK_KEY]
    return []


def commit_successful_watermarks_u2m(
    user_id: str,
    project_id: str,
    *,
    mode: str,
    trigger_type: str,
    completion_ts: float | None = None,
) -> None:
    """Persist watermarks only after a fully successful sync run."""
    ts = completion_ts if completion_ts is not None else time.time()
    keys = watermark_keys_for(mode, trigger_type)
    for key in keys:
        db.set_watermark(user_id, project_id, key, ts)

    logger.info(
        '[sync-watermark] committed mode=%s trigger=%s user=%s project=%s keys=%s ts=%s',
        mode,
        trigger_type,
        user_id,
        project_id,
        ','.join(keys) or mode,
        _format_anchor(ts),
    )
