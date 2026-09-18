import os





# ---------------------------------------------------------------------------
# Configuration — loaded from environment variables only
# ---------------------------------------------------------------------------
APS_BASE_AUTH    = 'https://developer.api.autodesk.com/authentication/v2'
APS_BASE_DM      = 'https://developer.api.autodesk.com/project/v1'
APS_BASE_PROFILE = 'https://developer.api.autodesk.com/userprofile/v1'

DC_BASE = 'https://developer.api.autodesk.com/data-connector/v1'
DC_SCHEMA_ENDPOINT = f"{DC_BASE}/doc/schema"


# Official standard service group enum (excluding the special ``all`` token).
# https://aps.autodesk.com/en/docs/acc/v1/reference/http/data-connector-requests-POST/
DC_STANDARD_SERVICE_GROUPS = [
    'admin', 'assets', 'checklists', 'cost', 'dailylogs',
    'forms', 'iq', 'issues', 'locations', 'markups', 'meetingminutes', 'photos',
    'relationships', 'reviews', 'rfis', 'schedule', 'sheets', 'submittals',
    'submittalsacc', 'transmittals',
]

# Full standard export — ``all`` alone; must NOT be combined with other groups.
DC_FULL_EXPORT_SERVICE_GROUPS = ['all']

# Standard + legacy groups with no CDC mirror — Pipeline A (snapshot-only) ownership.
# iq, markups, relationships stay snapshot-only until ACC exposes cdciq/cdcmarkups/cdcrelationships.
SNAPSHOT_ONLY_SERVICE_GROUPS = [
    'assets', 'checklists', 'clashes', 'classifications', 'dailylogs',
    'estimates', 'forms', 'iq', 'issuesbim360', 'markups', 'packages', 'photos',
    'relationships', 'reviews', 'submittals', 'takeoff',
]

# Default for dc_create_request() on Sync Snapshot (Pipeline A only).
# Lists snapshot-only groups explicitly — avoids downloading CDC-mirror domains
# that Pipeline B owns via cdc* exports.
# ACC rule: "use only 'all' OR specify each service group individually".
DC_DEFAULT_SNAPSHOT_EXPORT_GROUPS = list(SNAPSHOT_ONLY_SERVICE_GROUPS)

# Backward-compatible alias (explicit list, never includes ``all``).
DC_ALL_SERVICE_GROUPS = DC_STANDARD_SERVICE_GROUPS

# Delta extraction service groups (beta) — official ACC POST /requests enum (10 groups).
DC_CDC_SERVICE_GROUPS = [
    'cdcadmin', 'cdccost', 'cdcissues', 'cdclocations', 'cdcmeetingminutes',
    'cdcrfis', 'cdcschedule', 'cdcsubmittalsacc', 'cdcsheets', 'cdctransmittals',
]

# Standard groups that have a CDC mirror in the ACC API — Pipeline A skips; Pipeline B owns cdc*.
CDC_MIRROR_STANDARD_GROUPS = [
    'admin', 'cost', 'issues', 'locations', 'meetingminutes', 'rfis', 'schedule',
    'sheets', 'submittalsacc', 'transmittals',
]

# Legacy / extended schema groups in repo schemas/ but not in official enum.
DC_LEGACY_SERVICE_GROUPS = [
    'clashes', 'classifications', 'estimates', 'issuesbim360', 'packages',
    'takeoff',
]

DC_JOB_POLL_INTERVAL = 30    # seconds between job status polls
DC_JOB_MAX_WAIT      = 3600  # 1 hour max wait for job completion
DC_JOB_APPEAR_WAIT   = int(os.getenv('DC_JOB_APPEAR_WAIT', '1800'))  # wait for job under request (default 30 min)

TOKEN_REFRESH_BUFFER_SEC = 60   # rotate if token expires within 60 seconds
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 4]        # exponential backoff in seconds

# ---------------------------------------------------------------------------
# Secure Service Account (SSA) — headless ACC identity, one robot per hub.
# FR-03 §6.1 (app scopes), §7 (provisioning), §9 (JWT-bearer token minting).
# ---------------------------------------------------------------------------
APS_TOKEN_URL = f'{APS_BASE_AUTH}/token'
APS_SSA_BASE  = f'{APS_BASE_AUTH}/service-accounts'

SSA_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:jwt-bearer'

# APS rejects an assertion whose exp is more than 300s after iat. 120s leaves plenty of
# headroom for a slow round trip while keeping a stolen assertion short-lived. The iat is
# backdated so a server clock a little ahead of Autodesk's does not invalidate every mint.
SSA_ASSERTION_TTL_SEC = 120
SSA_CLOCK_SKEW_SEC    = 30

# FR-03 Appendix A — what the robot needs for Data Connector plus hub/project reads.
ACC_SSA_SCOPES = 'data:read data:write data:create data:search account:read'

# FR-03 §6.1 — scopes the vendor app itself needs to manage robots via the API.
SSA_MGMT_SCOPES = (
    'application:service_account:read '
    'application:service_account:write '
    'application:service_account_key:read '
    'application:service_account_key:write'
)

# Robot display name prefix; the APS-generated email derives from it and is what the hub
# admin invites to projects (FR-02 BR-10).
SSA_ROBOT_NAME_PREFIX = 'forma-dbx-'

