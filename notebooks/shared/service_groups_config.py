# Databricks notebook source
# ACC service group ownership — Pipeline A (snapshot-only) vs Pipeline B (CDC).
#
# Aligned with official ACC POST /requests serviceGroups enums:
# https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-POST/
#
# Note: ``all`` is a valid API request token but not a bronze schema name.

# Snapshot-only groups (Pipeline A owns these tables; no cdc* mirror).
SNAPSHOT_ONLY_GROUPS = frozenset({
    'assets', 'checklists', 'clashes', 'classifications', 'dailylogs',
    'estimates', 'forms', 'iq', 'issuesbim360', 'markups', 'packages', 'photos',
    'relationships', 'reviews', 'submittals', 'takeoff',
})

# Delta extraction service groups (beta) — official ACC API enum; Pipeline B only.
DELTA_CDC_GROUPS = frozenset({
    'cdcadmin', 'cdccost', 'cdcissues', 'cdclocations', 'cdcmeetingminutes',
    'cdcrfis', 'cdcschedule', 'cdcsubmittalsacc', 'cdcsheets', 'cdctransmittals',
})

# Standard domains with a CDC mirror in the ACC API — Pipeline A must skip these CSVs.
CDC_MIRROR_STANDARD_GROUPS = frozenset({
    'admin', 'cost', 'issues', 'locations', 'meetingminutes', 'rfis', 'schedule',
    'sheets', 'submittalsacc', 'transmittals',
})

ALL_STANDARD_GROUPS = SNAPSHOT_ONLY_GROUPS | CDC_MIRROR_STANDARD_GROUPS


def is_snapshot_only_schema(schema_name: str) -> bool:
    return schema_name in SNAPSHOT_ONLY_GROUPS


def is_delta_cdc_schema(schema_name: str) -> bool:
    return schema_name in DELTA_CDC_GROUPS


def schema_from_table_name(table_name: str, known_schemas: set) -> tuple[str, str]:
    """Split ``issues_attachments`` or ``cdcissues_issues`` into (schema, table)."""
    candidates = sorted(
        (s for s in known_schemas if table_name.startswith(s + '_')),
        key=len,
        reverse=True,
    )
    if candidates:
        sch = candidates[0]
        return sch, table_name[len(sch) + 1:]
    return 'unknown', table_name


def cdc_group_for_csv_stem(stem: str) -> str | None:
    """Return CDC service group if ``stem`` belongs to Pipeline B."""
    for grp in sorted(DELTA_CDC_GROUPS, key=len, reverse=True):
        if stem.startswith(grp + '_') or stem == grp:
            return grp
    return None
