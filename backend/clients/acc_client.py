"""
acc_client.py — Autodesk Platform Services (APS) 3-legged OAuth client.

Responsibilities:
  - Build the Autodesk authorization URL (redirect user to login)
  - Exchange authorization code for access + refresh tokens
  - Silently rotate access_token before every API call if near expiry
  - Fetch Hubs, Projects, Folders from APS Data Management API
  - Data Connector bulk export (create request, poll job, list/download files)
"""

import os
import logging
from .acc.auth_client import *

from .acc.project_client import *

from .acc.data_connector_client import *

from .acc.http_client import (
    _get,
    _post,
    _get_all_pages
)

from backend.repositories import state_store as db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Connector API — bulk async export of all service groups
# ---------------------------------------------------------------------------

def _acc_region() -> str:
    return os.getenv('ACC_REGION', 'US')


def _dc_headers_extra() -> dict:
    return {'region': _acc_region()}


