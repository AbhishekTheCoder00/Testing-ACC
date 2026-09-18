"""
ACC → Databricks Connector v2
Flask application — all routes and UI.

User flow:
  1. Connect ACC  (3-legged OAuth → hub/project selection)
  2. Connect Databricks  (Databricks OIDC → pick Unity Catalog → 10-step provisioning)
  3. Sync  (ACC API fetch → UC Volume → Bronze AUTO CDC pipeline)
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from backend.repositories import state_store as db

_ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
if _ENABLE_ZEROBUS:
    # from backend import zerobus_status
    from backend.services import zerobus_service
else:
    zerobus_service= None  # sentinel — guards every site that referenced the module
    # zerobus_status = None  # sentinel — guards every site that referenced the module

from flask import Blueprint

dashboard_bp = Blueprint(
    "dashboard",
    __name__
)

from backend.utils.auth import _current_user_id, _require_user_id


# ------------------------------------------------------------------
# Dashboard + Auto-Sync routes
# ------------------------------------------------------------------

def _parse_record_counts(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError, ValueError):
        return {}


def _csv_files_from_counts(rc: dict) -> int:
    v = rc.get('files')
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _run_display_number(run: dict) -> int:
    seq = run.get('user_run_seq')
    if seq is not None:
        return int(seq)
    return int(run.get('run_id') or 0)


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    """Dashboard-only aggregate: sync observability + Databricks landing context."""
    uid       = _require_user_id()
    acc_cfg   = db.get_acc_config(uid)
    latest    = db.get_latest_sync_run(uid)
    recent_pool = db.get_sync_history(uid, trigger_type=None, limit=50)
    auto_hist = db.get_sync_history(uid, trigger_type='auto', limit=5)
    bs        = db.get_bootstrap_state(uid)
    dbx_tok   = db.get_dbx_tokens(uid)

    latest_rc = _parse_record_counts(latest.get('record_counts') if latest else None)
    latest_files = _csv_files_from_counts(latest_rc)
    latest_dur_sec = None
    if latest and latest.get('state') in ('complete', 'failed'):
        latest_dur_sec = max(0, round(latest['updated_at'] - latest['started_at']))

    terminal = [r for r in recent_pool if r.get('state') in ('complete', 'failed')]
    ok_n = sum(1 for r in terminal if r.get('state') == 'complete')
    fail_n = sum(1 for r in terminal if r.get('state') == 'failed')
    denom = ok_n + fail_n
    reliability_pct = round(100.0 * ok_n / denom) if denom else None

    chrono = sorted(recent_pool, key=lambda r: (r.get('started_at') or 0, r.get('run_id') or 0))
    trend_slice = chrono[-20:] if len(chrono) > 20 else chrono
    chart_labels = []
    chart_files = []
    for r in trend_slice:
        rid = _run_display_number(r)
        ts = r.get('started_at')
        if ts:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            chart_labels.append(dt.strftime('%m/%d %H:%M') + f'  #{rid}')
        else:
            chart_labels.append(f'#{rid}')
        chart_files.append(_csv_files_from_counts(_parse_record_counts(r.get('record_counts'))))

    recent_runs = terminal[:10]

    base = (dbx_tok or {}).get('workspace_url', '').rstrip('/')
    # snapshot_pipeline_id replaces the legacy bronze_job_id after migration to
    # AUTO CDC FROM SNAPSHOT. Silver layer was removed in the same release.
    snapshot_pipeline_id = (bs or {}).get('snapshot_pipeline_id')
    cdc_pipeline_id    = (bs or {}).get('cdc_pipeline_id')
    links = {}
    if base:
        links['workspace'] = base
        if snapshot_pipeline_id:
            links['snapshot_pipeline'] = f'{base}/pipelines/{snapshot_pipeline_id}'
        if cdc_pipeline_id:
            links['cdc_pipeline'] = f'{base}/pipelines/{cdc_pipeline_id}'

    return jsonify({
        'hub_name':           acc_cfg.get('hub_name', '')     if acc_cfg else '',
        'project_name':       acc_cfg.get('project_name', '') if acc_cfg else '',
        'current_project_id': acc_cfg.get('project_id', '')   if acc_cfg else '',
        'latest_run':         latest,
        'recent_runs':        recent_runs,
        'manual_history':     recent_runs,
        'auto_history':       auto_hist,
        'stats': {
            'latest_csv_files':    latest_files,
            'latest_state':          latest.get('state') if latest else None,
            'latest_updated_at':     latest.get('updated_at') if latest else None,
            'latest_started_at':     latest.get('started_at') if latest else None,
            'latest_duration_sec':   latest_dur_sec,
            'reliability_pct':       reliability_pct,
            'terminal_run_count':    denom,
            'manual_success_rate_pct': reliability_pct,
            'manual_terminal_runs':    denom,
        },
        'chart': {
            'labels': chart_labels,
            'files':  chart_files,
        },
        'chart_outcomes': {
            'success': ok_n,
            'failed':  fail_n,
        },
        'databricks_context': {
            'workspace_url':      (dbx_tok or {}).get('workspace_url'),
            'cloud_provider':     (dbx_tok or {}).get('cloud_provider'),
            'catalog_name':       (bs or {}).get('catalog_name'),
            'volume_path':        (bs or {}).get('volume_path'),
            'snapshot_pipeline_id': snapshot_pipeline_id,
            'cdc_pipeline_id':    cdc_pipeline_id,
            'links':              links,
        },
    })
