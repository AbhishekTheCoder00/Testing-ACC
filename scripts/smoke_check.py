#!/usr/bin/env python3
"""Offline smoke checks — run from acc-connector/ with: python scripts/smoke_check.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.clients import acc_client
from backend.services import sync_service
from backend.repositories import state_store as db


def _check_m2m() -> list[str]:
    """Offline checks for the multi-tenant M2M path (Phase 1).

    Everything here is decidable without network or a real database: the schema initialises on
    a temp SQLite file, connection ids are deterministic, the memory SecretStore round-trips,
    and the in-flight state set is exactly the one FR-05 §10.3 names. A drift in any of those
    breaks tenant isolation or run bookkeeping, so they are worth catching before deploy.
    """
    import os
    import tempfile
    from unittest import mock

    errors: list[str] = []

    from backend.repositories.state import connection_sync_repository as run_repo
    from backend.repositories.state import database
    from backend.services.m2m.connection_identity import compute_connection_id

    # Schema initialises from scratch on a throwaway file, never the developer's connector.db.
    # ignore_cleanup_errors: on Windows the sqlite handle outlives the block, and a failed
    # temp-file delete must not fail the smoke check.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        with mock.patch.object(database, 'DB_PATH', os.path.join(tmp, 'smoke.db')):
            try:
                database.init_db()
                with database._conn() as con:
                    present = {
                        row['name'] for row in con.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'",
                        )
                    }
            except Exception as exc:
                present = set()
                errors.append(f'M2M schema init failed: {exc}')
            for table in (
                'aps_apps', 'tenants', 'tenant_users', 'ssa_credentials', 'connections',
                'connection_dbx_credentials', 'connection_bootstrap',
                'connection_sync_runs', 'connection_watermarks',
            ):
                if present and table not in present:
                    errors.append(f'missing state-store table {table}')

    first = compute_connection_id('b.hub1', 'p1', 'https://dbc-x.cloud.databricks.com', 'c1')
    second = compute_connection_id('b.hub1', 'p1', 'https://dbc-x.cloud.databricks.com/', 'c1')
    if first != second:
        errors.append('connection_id must ignore a trailing slash on the workspace URL')
    if not first.startswith('cnx_'):
        errors.append('connection_id must carry the cnx_ prefix')
    if first == compute_connection_id(
        'b.hub1', 'p1', 'https://dbc-x.cloud.databricks.com', 'c2',
    ):
        errors.append('connection_id must change with the catalog')

    if run_repo.IN_FLIGHT_STATES != frozenset(
        {'pending', 'exporting', 'downloading', 'pipeline_running'},
    ):
        errors.append('in-flight run states drifted from FR-05 §10.3')

    with mock.patch.dict(os.environ, {'SECRET_STORE_BACKEND': 'memory'}):
        from backend.secrets import get_secret_store, reset_secret_store
        from backend.secrets.secret_store import dbx_sp_secret_ref, ssa_private_key_ref

        reset_secret_store()
        try:
            store = get_secret_store()
            ref = ssa_private_key_ref('b.hub1')
            store.put_secret(ref, 'pem-material')
            if store.get_secret(ref) != 'pem-material':
                errors.append('SecretStore memory backend did not round-trip')
        except Exception as exc:
            errors.append(f'SecretStore round-trip failed: {exc}')
        finally:
            reset_secret_store()
        if dbx_sp_secret_ref('cnx_abc') == ssa_private_key_ref('cnx_abc'):
            errors.append('secret refs must not collide across credential kinds')

    from backend.services.m2m import connection_service
    if connection_service.WIZARD_STEPS[-1] != 'sync':
        errors.append('the wizard must end on the sync step')
    for status in ('pending_custom_integration', 'pending_ssa', 'pending_databricks',
                   'pending_bootstrap', 'ready'):
        if connection_service.next_step({'onboarding_status': status}) \
                not in connection_service.WIZARD_STEPS:
            errors.append(f'no wizard step for connection status {status}')

    from backend.services.m2m import connection_runner
    if connection_runner._STATE_MAP['complete'] != 'success':
        errors.append('a completed shared run must map to the success state')
    for state in connection_runner._STATE_MAP.values():
        if state not in run_repo.RUN_STATES:
            errors.append(f'run state {state} is not storable')

    # TC-MIG-04 — the single-tenant POC surface must stay deleted. A revert or a bad merge
    # bringing back the global-.env robot would quietly reintroduce the model this phase
    # replaced, and nothing else in the suite would notice.
    for gone in (
        'backend/services/m2m_service.py',
        'backend/routes/m2m_routes.py',
        'backend/repositories/state/m2m_repository.py',
    ):
        if (ROOT / gone).exists():
            errors.append(f'legacy single-tenant M2M module is back: {gone}')
    app_src = (ROOT / 'app.py').read_text(encoding='utf-8')
    if 'ENABLE_M2M' in app_src or 'm2m_routes' in app_src:
        errors.append('app.py still references the legacy M2M blueprint or flag')

    return errors


def main() -> int:
    errors: list[str] = []

    snap = acc_client.SNAPSHOT_ONLY_SERVICE_GROUPS
    cdc = acc_client.DC_CDC_SERVICE_GROUPS
    mirrors = acc_client.CDC_MIRROR_STANDARD_GROUPS
    expected_cdc = 10
    expected_snap = 16
    if len(cdc) != expected_cdc:
        errors.append(
            f'expected {expected_cdc} CDC service groups (official ACC enum), got {len(cdc)}',
        )
    if 'cdcissues' not in cdc:
        errors.append('cdcissues missing from DC_CDC_SERVICE_GROUPS')
    for unsupported in ('cdciq', 'cdcmarkups', 'cdcrelationships'):
        if unsupported in cdc:
            errors.append(
                f'{unsupported} must not be in DC_CDC_SERVICE_GROUPS (not in ACC API enum)',
            )
    for snapshot_std in ('iq', 'markups', 'relationships'):
        if snapshot_std not in snap:
            errors.append(f'{snapshot_std} missing from SNAPSHOT_ONLY_SERVICE_GROUPS')
        if snapshot_std in mirrors:
            errors.append(
                f'{snapshot_std} must not be a CDC mirror until ACC exposes cdc* export',
            )
    snap_mirror_overlap = set(snap) & set(mirrors)
    if snap_mirror_overlap:
        errors.append(
            'snapshot-only groups must not overlap CDC mirrors: '
            + ', '.join(sorted(snap_mirror_overlap)),
        )

    from backend.clients.acc.data_connector_client import _normalize_service_groups
    try:
        _normalize_service_groups(['all', 'admin'])
        errors.append('_normalize_service_groups should reject all+individual mix')
    except ValueError:
        pass
    if _normalize_service_groups(['all']) != ['all']:
        errors.append('_normalize_service_groups should pass through [all]')
    if _normalize_service_groups(None) != list(snap):
        errors.append('default snapshot export must match snapshot-only groups')

    if len(snap) != expected_snap:
        errors.append(f'expected {expected_snap} snapshot-only groups, got {len(snap)}')
    if acc_client.DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS != list(snap):
        errors.append('DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS must match SNAPSHOT_ONLY')
    for legacy in ('clashes', 'classifications', 'estimates', 'issuesbim360', 'packages', 'takeoff'):
        if legacy not in snap:
            errors.append(f'{legacy} missing from SNAPSHOT_ONLY_SERVICE_GROUPS')
    if 'all' in acc_client.DC_STANDARD_SERVICE_GROUPS:
        errors.append('DC_STANDARD_SERVICE_GROUPS must not include all')

    for name in ('run_sync', 'run_cdc_sync', '_run_dc_export'):
        if not hasattr(sync_service, name):
            errors.append(f'sync_service missing {name}')

    if sync_service.MANUAL_FULL_MIN_INTERVAL_SEC != 86400:
        errors.append('MANUAL_FULL_MIN_INTERVAL_SEC should be 86400')

    nb = ROOT / 'notebooks'
    for nb_name in (
        'acc_snapshot_pipeline.py',
        'acc_delta_cdc_pipeline.py',
        'acc_pipeline_test.py',
        'auto_cdc_pipeline.py',
        'auto_cdc_cdc_pipeline.py',
    ):
        if not (nb / nb_name).is_file():
            errors.append(f'missing notebook {nb_name}')

    for shared_name in ('service_groups_config.py', 'acc_pipeline_common.py'):
        if not (nb / 'shared' / shared_name).is_file():
            errors.append(f'missing shared notebook {shared_name}')

    snap_src = (nb / 'acc_snapshot_pipeline.py').read_text(encoding='utf-8')
    if 'create_auto_cdc_from_snapshot_flow' not in snap_src:
        errors.append('snapshot pipeline missing create_auto_cdc_from_snapshot_flow')
    if 'is_snapshot_only_schema' not in snap_src:
        errors.append('snapshot pipeline missing service group filter')
    if 'stored_as_scd_type=1' not in snap_src:
        errors.append('snapshot pipeline must use SCD Type 1')
    if 'stored_as_scd_type=2' in snap_src:
        errors.append('snapshot pipeline must not use SCD Type 2')

    cdc_src = (nb / 'acc_delta_cdc_pipeline.py').read_text(encoding='utf-8')
    if 'create_auto_cdc_flow' not in cdc_src:
        errors.append('CDC pipeline missing create_auto_cdc_flow')
    if 'once=True' not in cdc_src:
        errors.append('CDC pipeline missing ONCE flow')
    if 'create_auto_cdc_from_snapshot_flow' in cdc_src:
        errors.append('CDC pipeline should not use FROM SNAPSHOT')
    if 'stored_as_scd_type=1' not in cdc_src:
        errors.append('CDC pipeline must use SCD Type 1')
    if 'use_legacy_run_dir' not in cdc_src:
        errors.append('CDC pipeline missing legacy run-dir branch for changing acc.dc_cdc_path')
    if '_legacy_cdc_src' not in cdc_src:
        errors.append('CDC pipeline missing single legacy _legacy_cdc_src view')
    legacy_block = cdc_src.split('if use_legacy_run_dir:', 1)[-1].split('    else:', 1)[0]
    if 'once=True' in legacy_block or 'v_once_' in legacy_block:
        errors.append('legacy mode must not register separate ONCE flow (checkpoint conflict)')
    if 'read_csv_df(_glob' in cdc_src or 'read_csv_df(_path' in cdc_src:
        errors.append('CDC pipeline must not use batch read_csv_df in AUTO CDC views on SDP')
    if '_read_csv_stream' not in cdc_src:
        errors.append('CDC pipeline missing shared _read_csv_stream helper')
    if 'pathGlobFilter' not in cdc_src:
        errors.append('CDC readStream must use pathGlobFilter — load() requires a directory')
    if 'recursiveFileLookup' not in cdc_src:
        errors.append('CDC readStream must use recursiveFileLookup on project CDC root')
    if '_resolve_csv_stream_source' not in cdc_src:
        errors.append('CDC pipeline missing _resolve_csv_stream_source (dir + filter)')
    if "spark.readStream.format('csv')" not in cdc_src:
        errors.append('CDC pipeline missing readStream csv reader')

    common_src = (nb / 'shared' / 'acc_pipeline_common.py').read_text(encoding='utf-8')
    if 'resolve_project_cdc_root' not in common_src:
        errors.append('acc_pipeline_common missing resolve_project_cdc_root helper')
    if 'discover_cdc_csv_basenames' not in common_src:
        errors.append('acc_pipeline_common missing discover_cdc_csv_basenames helper')
    if not (nb / 'shared' / 'pk_audit_logic.py').is_file():
        errors.append('missing shared notebook pk_audit_logic.py')
    if 'audit_table_pk' not in cdc_src:
        errors.append('CDC pipeline missing audit_table_pk PK gate')
    if 'PK_AUDIT_BLOCK_STATUSES' not in common_src:
        errors.append('acc_pipeline_common missing PK_AUDIT_BLOCK_STATUSES')
    if '_ensure_run_counts' not in common_src:
        errors.append('acc_pipeline_common missing _ensure_run_counts')
    if '__START_AT' in common_src:
        errors.append('acc_pipeline_common must not add SCD2 history columns')

    from backend.services.pipeline_naming import (
        snapshot_pipeline_name,
        cdc_pipeline_name,
    )
    sample_snap = snapshot_pipeline_name('test_catalog')
    sample_cdc = cdc_pipeline_name('test_catalog')
    if 'PipelineA' not in sample_snap:
        errors.append('snapshot pipeline name should include PipelineA')
    if 'PipelineB' not in sample_cdc:
        errors.append('CDC pipeline name should include PipelineB')

    test_src = (nb / 'acc_pipeline_test.py').read_text(encoding='utf-8')
    if 'acc.test_mode' not in test_src:
        errors.append('test pipeline missing acc.test_mode')

    from backend.routes.sync_routes import _timer_key, rehydrate_auto_sync_timers
    if _timer_key('user_a', 'proj_x') == _timer_key('user_b', 'proj_x'):
        errors.append('_timer_key must isolate Auto CDC timers per user')
    if not callable(rehydrate_auto_sync_timers):
        errors.append('rehydrate_auto_sync_timers missing for restart recovery')

    try:
        db.init_db()
    except Exception as exc:
        errors.append(f'init_db failed: {exc}')

    errors.extend(_check_m2m())

    if errors:
        print('SMOKE FAILED:')
        for e in errors:
            print(f'  - {e}')
        return 1

    print('SMOKE OK — offline checks passed')
    print(f'  CDC groups: {len(acc_client.DC_CDC_SERVICE_GROUPS)}')
    print(f'  Snapshot-only groups: {len(snap)}')
    print(f'  Snapshot export default: {len(acc_client.DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS)} groups')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
