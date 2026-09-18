
import logging
from .constants import *
from .http_client import (  _get, _get_all_pages )

from backend.clients.acc.auth_client import (
    _get_2legged_token,
    get_valid_token,
)

logger = logging.getLogger(__name__)

############################################

# ---------------------------------------------------------------------------
# Data Management — Hubs / Projects / Folders
# ---------------------------------------------------------------------------

def fetch_hubs(user_id: str) -> list:
    """Return merged hub list from both 2-legged (app-provisioned) and
    3-legged (user-visible) tokens, with pagination support.

    - 2-legged covers hubs where the APS app is provisioned via ACC Admin.
    - 3-legged covers additional hubs the logged-in user can see (e.g. hubs
      where they are Project Admin but not Hub Admin).
    Merging by hub ID avoids duplicates.
    """
    hubs_by_id: dict = {}

    # Only the 3-legged (signed-in user) token — returns exactly the hubs the
    # user is allowed to see. The 2-legged (app) token is intentionally NOT
    # merged in: it lists every app-provisioned hub regardless of user access,
    # which would leak hubs the user has no access to.
    try:
        three_leg = get_valid_token(user_id)
        for hub in _get_all_pages(f'{APS_BASE_DM}/hubs', three_leg):
            hubs_by_id[hub['id']] = hub
        logger.info('3-legged hub fetch: %d hubs visible to user', len(hubs_by_id))
    except Exception as exc:
        logger.warning('3-legged hub fetch failed: %s', exc)

    logger.info('Total hubs for user: %d', len(hubs_by_id))
    return [
        {
            'id':   hub['id'],
            'name': hub['attributes']['name'],
            'type': hub['attributes'].get('extension', {}).get('type', ''),
        }
        for hub in hubs_by_id.values()
    ]


def fetch_projects(user_id: str, hub_id: str) -> list:
    """Return list of projects in the given hub.

    Uses the 3-legged (user) token only, so it returns exactly the projects the
    logged-in user is a member of. The 2-legged (app) fallback is intentionally
    NOT used: it would surface projects in hubs the user has no access to.
    """
    three_leg = get_valid_token(user_id)
    items = _get_all_pages(f'{APS_BASE_DM}/hubs/{hub_id}/projects', three_leg)
    logger.info('Hub %s: %d projects found', hub_id, len(items))
    return [
        {
            'id':   proj['id'],
            'name': proj['attributes']['name'],
        }
        for proj in items
    ]


def fetch_top_folders(user_id: str, hub_id: str, project_id: str) -> list:
    """Return top-level folders for the given project."""
    token = get_valid_token(user_id)
    data = _get(f'{APS_BASE_DM}/hubs/{hub_id}/projects/{project_id}/topFolders', token)
    return [
        {
            'id':   folder['id'],
            'name': folder['attributes']['name'],
        }
        for folder in data.get('data', [])
    ]


# ---------------------------------------------------------------------------
# User Projects — all projects the logged-in user has access to
# ---------------------------------------------------------------------------

def fetch_user_projects(user_id: str) -> list:
    """Return flat list of all projects the logged-in user is a member of.

    Flow:
      1. GET /project/v1/hubs — merge 2-legged (app-provisioned) + 3-legged (user hubs)
      2. GET /project/v1/hubs/{hubId}/projects with 3-legged token per hub
         (3-legged returns only projects the user is a member of)

    Production note: to filter by Project Admin role, use:
      GET /construction/admin/v1/projects/{projectId}/users — requires account admin rights.
      Alternatively: GET /construction/admin/v1/accounts/{accountId}/users/{userId}/products
      with filter[key]=build — also requires account admin.
    """
    token = get_valid_token(user_id)

    # Step 1 — hubs the signed-in user can access (3-legged only). The 2-legged
    # (app) token is intentionally NOT merged in, as it would leak hubs the user
    # has no access to.
    hubs_by_id = {}
    try:
        for hub in _get(f'{APS_BASE_DM}/hubs', token).get('data', []):
            hubs_by_id[hub['id']] = hub
    except Exception as exc:
        logger.warning('3-legged hub fetch failed: %s', exc)

    hubs = list(hubs_by_id.values())
    logger.info('Fetched %d hubs visible to user', len(hubs))

    all_projects = []

    for hub in hubs:
        hub_id   = hub['id']
        hub_name = hub['attributes']['name']

        # Step 2 — get projects user is member of in this hub (3-legged only)
        try:
            proj_data = _get(f'{APS_BASE_DM}/hubs/{hub_id}/projects', token)
            for proj in proj_data.get('data', []):
                all_projects.append({
                    'id':       proj['id'],
                    'name':     proj['attributes']['name'],
                    'hub_id':   hub_id,
                    'hub_name': hub_name,
                })
            logger.info('Hub %s: %d accessible projects', hub_name, len(proj_data.get('data', [])))
        except Exception as exc:
            logger.warning('Could not fetch projects for hub %s: %s', hub_id, exc)

    logger.info('Total accessible projects found: %d', len(all_projects))
    return all_projects

