"""
zerobus_status.py — Zerobus prerequisite diagnostics (ENABLE_ZEROBUS only).

Exposes ``check_zerobus_prerequisites(user_id)`` which the Flask layer
serves via ``GET /sync/zerobus-status``. The result is a structured dict
the UI uses to render a green "ready" banner or a red banner naming the
specific blocker.

Checks performed (each independent, all run even if earlier ones fail):
  1. Databricks token exists and is valid
  2. Bootstrap state is present and a catalog was selected
  3. The selected catalog is reachable
  4. The selected catalog has an explicit storage_root (managed catalog
     with explicit storage — Default Storage catalogs are NOT eligible)
  5. The Service Principal in .env (DATABRICKS_CLIENT_ID) has the
     required grants on the catalog and on its bronze schema

The aggregator never raises — it returns ``checks`` and a top-level
``ready`` boolean. Errors are surfaced as ``ok=False`` entries with
human-readable messages and, where useful, a copyable ``fix_sql`` value.
"""

from __future__ import annotations

import logging

# from . import state_store as db

from backend.config import get_config
from backend.repositories import state_store as db
# 
# from .databricks_client import DatabricksClient
from backend.clients.databricks_client import DatabricksClient

logger = logging.getLogger(__name__)

# Privileges required for Zerobus to write to <catalog>.bronze.acc_*.
# These mirror the GRANT statements documented in SESSION_STATE.md.
REQUIRED_CATALOG_PRIVILEGES = {'USE_CATALOG'}
REQUIRED_SCHEMA_PRIVILEGES = {'USE_SCHEMA', 'CREATE_TABLE', 'MODIFY'}

BRONZE_SCHEMA = 'bronze'

# Special-cased catalog name used by the bulk path on Default Storage
# workspaces. Surface a helpful hint when the user is on it.
DEFAULT_STORAGE_HINT = (
    'The selected catalog uses Default Storage and cannot host Zerobus '
    'writes. Re-bootstrap against a catalog created with explicit S3 '
    'storage (e.g. CREATE CATALOG <name> MANAGED LOCATION \'s3://...\')'
    ' or pick an existing managed catalog from the bootstrap dropdown.'
)


def _check(ok: bool, message: str, **extra) -> dict:
    """Build a uniform check-entry dict."""
    out = {'ok': bool(ok), 'message': message}
    out.update(extra)
    return out


def _principal_id() -> str:
    """Return the Databricks principal we expect to find in the grants list.

    For OAuth applications / Service Principals registered in a Databricks
    account, the principal in SHOW GRANTS / permissions API responses is the
    application id (UUID), which is the same value as DATABRICKS_CLIENT_ID
    in our .env.
    """
    return get_config('DATABRICKS_CLIENT_ID', '').strip()


def _principal_matches(entry_principal: str, target_principal: str) -> bool:
    """Tolerant principal-name matcher.

    Databricks UC may report the same Service Principal in any of these forms
    depending on how the GRANT was issued and the API version:
      * the application_id UUID (what we hand to GRANT and what is in .env)
      * the SP display name (e.g. "ACC Connector App")
      * the UUID with leading/trailing whitespace
      * different case for the alphabetic portion of the UUID

    We accept a match when either side, lowercased and stripped, equals the
    other OR contains the other. Display-name-only grants will still miss,
    which is intentional — those are surfaced via the raw principals_seen
    list in the diagnostic so the user can see what UC actually has on file.
    """
    a = (entry_principal or '').strip().lower()
    b = (target_principal or '').strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _missing_privileges(assignments: list, principal: str,
                        required: set[str]) -> set[str]:
    """Return the subset of ``required`` privileges NOT granted to ``principal``."""
    granted: set[str] = set()
    for entry in assignments or []:
        ent_principal = (entry.get('principal') or '').strip()
        if not _principal_matches(ent_principal, principal):
            continue
        for priv in entry.get('privileges') or []:
            # API returns enum-style names like "USE_CATALOG", "MODIFY"
            granted.add(str(priv).upper().replace(' ', '_'))
    return required - granted


def _principals_seen(assignments: list) -> list[str]:
    """Return the unique principal names present in the grants response.

    Surfaced in the diagnostic so the user can see at a glance whether ANY
    grant exists on the securable yet, and under which principal identifier
    UC has filed it.
    """
    seen: list[str] = []
    for entry in assignments or []:
        p = (entry.get('principal') or '').strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def check_zerobus_prerequisites(user_id: str) -> dict:
    """Run every Zerobus prerequisite check for ``user_id`` and aggregate."""
    checks: dict[str, dict] = {}
    catalog_name: str | None = None
    storage_root: str | None = None

    # ------------------------------------------------------------------
    # Check 1 — Databricks token exists
    # ------------------------------------------------------------------
    dbx_tok = db.get_dbx_tokens(user_id)
    if not dbx_tok:
        checks['databricks_connected'] = _check(
            False,
            'Databricks is not connected. Complete Step 2 (Connect Databricks) first.',
        )
        return _aggregate(catalog=None, checks=checks)
    checks['databricks_connected'] = _check(
        True, f'Connected to {dbx_tok["workspace_url"]}'
    )

    # ------------------------------------------------------------------
    # Check 2 — Bootstrap state present
    # ------------------------------------------------------------------
    bs_state = db.get_bootstrap_state(user_id)
    if not bs_state:
        checks['bootstrap_complete'] = _check(
            False,
            'Bootstrap has not run yet. Complete Step 2 (Connect Databricks → Provisioning) first.',
        )
        return _aggregate(catalog=None, checks=checks)
    catalog_name = (bs_state.get('catalog_name') or '').strip() or None
    if not catalog_name:
        checks['bootstrap_complete'] = _check(
            False,
            'Bootstrap state is missing catalog_name. Re-run bootstrap.',
        )
        return _aggregate(catalog=None, checks=checks)
    checks['bootstrap_complete'] = _check(
        True, f'Catalog selected at bootstrap: {catalog_name}'
    )

    # Build a Databricks client for the remaining checks. Network errors
    # here become blocking and stop further checks (each subsequent check
    # would hit the same failure).
    dbx = DatabricksClient(dbx_tok['workspace_url'], dbx_tok['access_token'])

    # ------------------------------------------------------------------
    # Check 3 — Catalog reachable
    # ------------------------------------------------------------------
    try:
        catalog = dbx.uc_get_catalog(catalog_name)
        checks['catalog_accessible'] = _check(
            True, f'Catalog {catalog_name} is accessible.'
        )
    except Exception as exc:
        checks['catalog_accessible'] = _check(
            False,
            f'Cannot access catalog {catalog_name}: {exc}',
        )
        return _aggregate(catalog=catalog_name, checks=checks)

    # ------------------------------------------------------------------
    # Check 4 — Catalog has explicit storage_root (managed catalog)
    # ------------------------------------------------------------------
    storage_root = (catalog.get('storage_root') or '').strip() or None
    if storage_root:
        checks['managed_catalog'] = _check(
            True,
            f'Managed catalog with storage_root: {storage_root}',
            storage_root=storage_root,
        )
    else:
        checks['managed_catalog'] = _check(
            False,
            'This is not a managed catalog. ' + DEFAULT_STORAGE_HINT,
        )
        # Subsequent checks still run — grants info is useful regardless.

    # ------------------------------------------------------------------
    # Check 5 — Service Principal grants on catalog + bronze schema
    # ------------------------------------------------------------------
    principal = _principal_id()
    if not principal:
        checks['service_principal_grants'] = _check(
            False,
            'DATABRICKS_CLIENT_ID is not set in .env. Cannot verify Service '
            'Principal grants without it.',
        )
    else:
        missing_catalog: set[str] = set()
        missing_schema: set[str] = set()
        catalog_check_failed = False
        schema_check_failed = False
        catalog_principals: list[str] = []
        schema_principals: list[str] = []

        # Fetch ALL grants (no principal filter) — the server-side filter has
        # historically been case-/format-sensitive; doing the matching ourselves
        # with _principal_matches lets us handle the edge cases.
        try:
            cat_perms = dbx.uc_get_permissions('catalog', catalog_name)
            cat_assignments = cat_perms.get('privilege_assignments') or []
            missing_catalog = _missing_privileges(
                cat_assignments, principal, REQUIRED_CATALOG_PRIVILEGES
            )
            catalog_principals = _principals_seen(cat_assignments)
        except Exception as exc:
            catalog_check_failed = True
            logger.warning('SP grants check on catalog failed: %s', exc)

        bronze_full = f'{catalog_name}.{BRONZE_SCHEMA}'
        try:
            schema_perms = dbx.uc_get_permissions('schema', bronze_full)
            schema_assignments = schema_perms.get('privilege_assignments') or []
            missing_schema = _missing_privileges(
                schema_assignments, principal, REQUIRED_SCHEMA_PRIVILEGES
            )
            schema_principals = _principals_seen(schema_assignments)
        except Exception as exc:
            # Most common reason: bronze schema doesn't exist yet. That's
            # fine for the diagnostic — bootstrap or a future Zerobus writer will
            # create it. We surface this as a separate informational check.
            schema_check_failed = True
            logger.info('SP grants check on bronze schema failed: %s', exc)

        if catalog_check_failed and schema_check_failed:
            checks['service_principal_grants'] = _check(
                False,
                'Could not read grant information from Unity Catalog. '
                'The connected user may lack MANAGE permission to view grants. '
                'Try running SHOW GRANTS manually in Databricks SQL.',
            )
        elif missing_catalog or missing_schema or schema_check_failed:
            # Build a helpful fix_sql that re-runs ONLY the missing grants.
            fix_lines: list[str] = []
            if missing_catalog:
                fix_lines.append(
                    f'GRANT {", ".join(sorted(missing_catalog))} '
                    f'ON CATALOG {catalog_name} '
                    f'TO `{principal}`;'
                )
            if missing_schema or schema_check_failed:
                privs = ', '.join(sorted(missing_schema or REQUIRED_SCHEMA_PRIVILEGES))
                fix_lines.append(
                    f'GRANT {privs} '
                    f'ON SCHEMA {bronze_full} '
                    f'TO `{principal}`;'
                )
            if schema_check_failed and not missing_schema:
                # Schema may not exist yet — include a CREATE-schema hint.
                fix_lines.insert(
                    0,
                    f'-- Bronze schema may not exist yet; create it then re-grant:\n'
                    f'CREATE SCHEMA IF NOT EXISTS {bronze_full};',
                )
            checks['service_principal_grants'] = _check(
                False,
                f'Service Principal {principal} is missing required grants.',
                principal=principal,
                missing_catalog_privileges=sorted(missing_catalog),
                missing_schema_privileges=sorted(missing_schema),
                bronze_schema_unreachable=schema_check_failed,
                fix_sql='\n'.join(fix_lines),
                principals_with_catalog_grants=catalog_principals,
                principals_with_schema_grants=schema_principals,
            )
        else:
            checks['service_principal_grants'] = _check(
                True,
                f'Service Principal {principal} has the required grants.',
                principal=principal,
            )

    return _aggregate(catalog=catalog_name, checks=checks,
                      storage_root=storage_root)


def _aggregate(catalog: str | None, checks: dict[str, dict],
               storage_root: str | None = None) -> dict:
    """Build the top-level response — derive ``ready`` and a banner message."""
    all_ok = bool(checks) and all(c.get('ok') for c in checks.values())

    # Pick the first failing check (in declared order) as the headline issue.
    blocking_message = None
    if not all_ok:
        for key in (
            'databricks_connected',
            'bootstrap_complete',
            'catalog_accessible',
            'managed_catalog',
            'service_principal_grants',
        ):
            entry = checks.get(key)
            if entry and not entry.get('ok'):
                blocking_message = entry.get('message')
                break

    return {
        'ready':            all_ok,
        'catalog':          catalog,
        'storage_root':     storage_root,
        'principal':        _principal_id() or None,
        'checks':           checks,
        'blocking_message': blocking_message,
    }
