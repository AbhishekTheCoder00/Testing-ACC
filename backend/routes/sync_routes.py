"""
ACC → Databricks Connector v2
Flask application — all routes and UI.

User flow:
  1. Connect ACC  (3-legged OAuth → hub/project selection)
  2. Connect Databricks  (Databricks OIDC → pick Unity Catalog → 10-step provisioning)
  3. Sync  (ACC API fetch → UC Volume → Bronze AUTO CDC pipeline)
"""


import logging
import os
import threading
import time
from flask import Flask, abort, jsonify, request, session
from backend.repositories import state_store as db

from flask import Blueprint

sync_bp = Blueprint(
    "sync",
    __name__
)

from backend.services import sync_service
from backend.services.sync.sync_reconcile_service import reconcile_u2m_sync_run

_ENABLE_ZEROBUS = os.getenv('ENABLE_ZEROBUS', 'false').lower() == 'true'
if _ENABLE_ZEROBUS:
    # from backend import zerobus_status
    from backend.services import zerobus_service
else:
    zerobus_service= None  # sentinel — guards every site that referenced the module
    # zerobus_status = None  # sentinel — guards every site that referenced the module


logger = logging.getLogger(__name__)

AUTO_SYNC_INTERVAL = 24 * 3600  # 24 hours — daily CDC-beta sync
_auto_sync_timers = {}     # { (user_id, project_id): { timer, hub_name, project_name, ... } }
_auto_sync_lock   = threading.Lock()

from backend.utils.auth import _current_user_id, _require_user_id


def _timer_key(user_id: str, project_id: str) -> tuple[str, str]:
    """Composite key so multiple tenants can enable Auto CDC for the same ACC project."""
    return (user_id, project_id)


def _catalog_ownership_error(user_id: str) -> str | None:
    """Return an error message if the caller no longer owns their catalog.

    Guards against a session that was provisioned before its claim was
    released or handed to someone else — without this, that user's sync would
    keep driving pipelines the new owner now controls. An *unclaimed* catalog
    is allowed: legacy rows whose claim could not be backfilled must keep
    working.
    """
    bs_state = db.get_bootstrap_state(user_id)
    catalog_name = (bs_state or {}).get('catalog_name')
    if not catalog_name:
        return None
    dbx_tok = db.get_dbx_tokens(user_id)
    workspace_key = db.normalize_workspace_key((dbx_tok or {}).get('workspace_url'))
    if not workspace_key:
        return None
    if db.owns_catalog(workspace_key, catalog_name, user_id):
        return None
    return (
        f'Catalog "{catalog_name}" is now provisioned by another user. '
        f'Pick a different catalog in Step 2.'
    )


def _persist_auto_cdc_next_run(user_id: str, next_run_at: float) -> None:
    db.set_auto_cdc_next_run_at(user_id, next_run_at)


def _auto_cdc_schedule(uid: str, project_id: str, cfg: dict | None) -> tuple[float | None, float | None]:
    """Return (last_auto_run_at, next_run_at) for UI — survives connector restart."""
    last_auto = db.get_latest_sync_for_project(uid, project_id, trigger_type='auto')
    last_at = last_auto.get('updated_at') if last_auto else None

    next_at = None
    key = _timer_key(uid, project_id)
    with _auto_sync_lock:
        entry = _auto_sync_timers.get(key)
        if entry:
            next_at = entry.get('next_run_at')
    if not next_at and cfg:
        stored = cfg.get('auto_cdc_next_run_at')
        if stored:
            next_at = float(stored)
    if not next_at and last_at:
        next_at = last_at + AUTO_SYNC_INTERVAL
    return last_at, next_at


def _start_auto_sync(user_id, project_id, hub_name, project_name):
    key = _timer_key(user_id, project_id)
    next_run_at = time.time() + AUTO_SYNC_INTERVAL
    with _auto_sync_lock:
        if key in _auto_sync_timers:
            return False
        t = threading.Timer(
            AUTO_SYNC_INTERVAL, _auto_sync_tick, args=(user_id, project_id),
        )
        t.daemon = True
        t.start()
        _auto_sync_timers[key] = {
            'timer':        t,
            'hub_name':     hub_name,
            'project_name': project_name,
            'started_at':   time.time(),
            'next_run_at':  next_run_at,
        }
    _persist_auto_cdc_next_run(user_id, next_run_at)
    logger.info(
        '[auto-sync] Enabled daily CDC sync for user %s project %s',
        user_id, project_id,
    )
    return True


def _stop_auto_sync(user_id, project_id):
    """Cancel the daily timer for one tenant's project."""
    key = _timer_key(user_id, project_id)
    with _auto_sync_lock:
        entry = _auto_sync_timers.pop(key, None)
        if entry is None:
            return False
        if entry.get('timer'):
            entry['timer'].cancel()
    logger.info(
        '[auto-sync] Disabled daily sync for user %s project %s',
        user_id, project_id,
    )
    return True


def stop_all_auto_sync_for_user(user_id: str) -> int:
    """Cancel every in-memory daily CDC timer for one user (e.g. on Start over)."""
    with _auto_sync_lock:
        keys = [project_id for (uid, project_id) in _auto_sync_timers if uid == user_id]
    stopped = 0
    for project_id in keys:
        if _stop_auto_sync(user_id, project_id):
            stopped += 1
    if stopped:
        logger.info(
            '[auto-sync] Stopped %d daily CDC timer(s) for user %s',
            stopped, user_id,
        )
    return stopped


def _auto_sync_tick(user_id: str, project_id: str):
    """Execute one auto-sync cycle and reschedule."""
    key = _timer_key(user_id, project_id)
    with _auto_sync_lock:
        if key not in _auto_sync_timers:
            return
    try:
        logger.info(
            '[auto-sync] Running daily CDC sync for user %s project %s',
            user_id, project_id,
        )
        sync_service.run_cdc_sync(user_id, trigger_type='auto')
    except Exception as exc:
        logger.error(
            '[auto-sync] Error for user %s project %s: %s',
            user_id, project_id, exc,
        )

    with _auto_sync_lock:
        if key in _auto_sync_timers:
            t = threading.Timer(
                AUTO_SYNC_INTERVAL, _auto_sync_tick, args=(user_id, project_id),
            )
            t.daemon = True
            t.start()
            next_run_at = time.time() + AUTO_SYNC_INTERVAL
            _auto_sync_timers[key]['timer'] = t
            _auto_sync_timers[key]['next_run_at'] = next_run_at
            _persist_auto_cdc_next_run(user_id, next_run_at)


def rehydrate_auto_sync_timers() -> None:
    """Restore in-memory daily CDC timers from SQLite after connector restart."""
    configs = db.list_daily_cdc_configs()
    if not configs:
        return

    restored = 0
    for cfg in configs:
        user_id = cfg['user_id']
        project_id = cfg.get('project_id') or ''
        if not project_id:
            logger.warning('[auto-sync] Rehydrate skip user %s: no project_id', user_id)
            db.set_daily_cdc_enabled(user_id, False)
            continue
        if _catalog_ownership_error(user_id):
            logger.warning(
                '[auto-sync] Rehydrate skip user %s: catalog no longer owned',
                user_id,
            )
            db.set_daily_cdc_enabled(user_id, False)
            continue
        bs = db.get_bootstrap_state(user_id)
        if not bs or not bs.get('cdc_pipeline_id'):
            logger.warning(
                '[auto-sync] Rehydrate skip user %s: CDC pipeline not provisioned',
                user_id,
            )
            continue
        if not db.has_successful_snapshot(user_id, project_id):
            logger.warning(
                '[auto-sync] Rehydrate skip user %s: no snapshot baseline',
                user_id,
            )
            db.set_daily_cdc_enabled(user_id, False)
            continue
        if _start_auto_sync(
            user_id,
            project_id,
            cfg.get('hub_name', ''),
            cfg.get('project_name', ''),
        ):
            restored += 1

    logger.info('[auto-sync] Rehydrated %d daily CDC timer(s)', restored)


# ------------------------------------------------------------------
# Sync
# ------------------------------------------------------------------

@sync_bp.route('/sync', methods=['POST'])
def start_sync():
    """Legacy alias — same as Sync Snapshot."""
    return start_sync_snapshot()


@sync_bp.route('/sync/snapshot', methods=['POST'])
def start_sync_snapshot():
    """Sync Snapshot: snapshot-only groups + cdc* baseline → Pipeline A + B."""
    uid = _require_user_id()
    ownership_error = _catalog_ownership_error(uid)
    if ownership_error:
        return jsonify({'error': ownership_error}), 403

    def _run():
        try:
            sync_service.run_sync(uid)
        except Exception as exc:
            logger.error('Background snapshot sync error: %s', exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({'message': 'Sync Snapshot started'}), 200


@sync_bp.route('/sync/cdc', methods=['POST'])
def start_sync_cdc():
    """Sync CDC: cdc* groups → Pipeline B ongoing flows only."""
    uid = _require_user_id()
    ownership_error = _catalog_ownership_error(uid)
    if ownership_error:
        return jsonify({'error': ownership_error}), 403
    cfg = db.get_acc_config(uid)
    if not cfg:
        return jsonify({'error': 'ACC not configured'}), 400
    bs = db.get_bootstrap_state(uid)
    if not bs or not bs.get('cdc_pipeline_id'):
        return jsonify({'error': 'CDC pipeline not provisioned — re-run bootstrap.'}), 400
    if not db.has_successful_snapshot(uid, cfg['project_id']):
        return jsonify({
            'error': 'Complete Sync Snapshot successfully before running Sync CDC.',
        }), 409
    if not db.is_daily_cdc_enabled(uid):
        return jsonify({'error': 'Enable Auto CDC before running Sync CDC.'}), 409

    def _run():
        try:
            sync_service.run_cdc_sync(uid, trigger_type='manual')
        except Exception as exc:
            logger.error('Background CDC sync error: %s', exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({'message': 'Sync CDC started'}), 200


@sync_bp.route('/sync/status')
def sync_status():
    uid = _require_user_id()
    cfg = db.get_acc_config(uid)
    project_id = cfg['project_id'] if cfg else None
    snapshot_run = None
    cdc_run = None
    if project_id:
        snapshot_run = db.get_latest_snapshot_run(uid, project_id)
        cdc_run = db.get_latest_cdc_run(uid, project_id)
        if snapshot_run:
            snapshot_run = reconcile_u2m_sync_run(uid, snapshot_run)
        if cdc_run:
            cdc_run = reconcile_u2m_sync_run(uid, cdc_run)
        db.has_in_flight_sync_run(uid)
    manual_full_next_at = None
    auto_cdc_last_run_at = None
    auto_cdc_next_run_at = None
    if cfg:
        last_full = db.get_watermark(
            uid, cfg['project_id'], sync_service.MANUAL_FULL_WATERMARK_KEY,
        )
        if last_full:
            manual_full_next_at = last_full + sync_service.MANUAL_FULL_MIN_INTERVAL_SEC
        if cfg.get('daily_cdc_enabled') and project_id:
            auto_cdc_last_run_at, auto_cdc_next_run_at = _auto_cdc_schedule(
                uid, project_id, cfg,
            )
    return jsonify({
        'run':                   snapshot_run,
        'snapshot_run':          snapshot_run,
        'cdc_run':               cdc_run,
        'hub_name':              cfg.get('hub_name', '')     if cfg else '',
        'project_name':          cfg.get('project_name', '') if cfg else '',
        'daily_cdc_enabled':     bool(cfg.get('daily_cdc_enabled')) if cfg else False,
        'auto_cdc_last_run_at':  auto_cdc_last_run_at,
        'auto_cdc_next_run_at':  auto_cdc_next_run_at,
        'manual_full_next_at':   manual_full_next_at,
        'has_snapshot_baseline': bool(
            cfg and db.has_successful_snapshot(uid, cfg['project_id'])
        ),
    })


if _ENABLE_ZEROBUS:
    @sync_bp.route('/sync/zerobus-status')
    # def sync_zerobus_status():
    def sync_zerobus_service():
        """Return a structured diagnostic of Zerobus prerequisites.

        Only registered when ENABLE_ZEROBUS=true. Never raises; failures
        are surfaced as ``ok=False`` entries inside ``checks``.
        """
        uid = _require_user_id()
        try:
            result = zerobus_service.check_zerobus_prerequisites(uid)
            return jsonify(result)
        except Exception as e:
            logger.exception('zerobus-status aggregator failed unexpectedly')
            return jsonify({
                'ready':            False,
                'catalog':          None,
                'storage_root':     None,
                'principal':        None,
                'checks':           {},
                'blocking_message': f'Diagnostic failed: {e}',
            }), 500




@sync_bp.route('/sync/auto', methods=['GET', 'POST'])
def sync_auto():
    uid     = _require_user_id()
    acc_cfg = db.get_acc_config(uid)

    if request.method == 'GET':
        active = []
        with _auto_sync_lock:
            for (owner_uid, pid), entry in _auto_sync_timers.items():
                if owner_uid != uid:
                    continue
                last_run = db.get_latest_sync_for_project(uid, pid, trigger_type='auto')
                active.append({
                    'project_id':   pid,
                    'hub_name':     entry.get('hub_name', ''),
                    'project_name': entry.get('project_name', ''),
                    'last_run_state': last_run.get('state') if last_run else None,
                    'last_run_at':    last_run.get('updated_at') if last_run else None,
                    'next_run_at':    entry.get('next_run_at'),
                    'started_at':     entry.get('started_at'),
                })
        return jsonify({'active_syncs': active})

    body    = request.json or {}
    enabled = body.get('enabled', True)

    if enabled:
        ownership_error = _catalog_ownership_error(uid)
        if ownership_error:
            return jsonify({'error': ownership_error}), 403
        if not acc_cfg:
            return jsonify({'error': 'ACC not configured'}), 400
        bs = db.get_bootstrap_state(uid)
        if not bs or not bs.get('cdc_pipeline_id'):
            return jsonify({
                'error': 'CDC pipeline not provisioned — re-run bootstrap.',
            }), 400
        project_id   = body.get('project_id', acc_cfg.get('project_id', ''))
        if not db.has_successful_snapshot(uid, project_id):
            return jsonify({
                'error': 'Complete Sync Snapshot before enabling Auto CDC.',
            }), 409
        hub_name     = acc_cfg.get('hub_name', '')
        project_name = acc_cfg.get('project_name', '')
        ok = _start_auto_sync(uid, project_id, hub_name, project_name)
        if not ok:
            return jsonify({'error': 'Daily CDC sync already active for this project'}), 409
        db.set_daily_cdc_enabled(uid, True)
        return jsonify({'message': f'Daily CDC sync enabled for {project_name}'})
    else:
        project_id = body.get('project_id', acc_cfg.get('project_id', '') if acc_cfg else '')
        _stop_auto_sync(uid, project_id)
        db.set_daily_cdc_enabled(uid, False)
        return jsonify({'message': 'Daily CDC sync disabled'})
