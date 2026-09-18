"""Unit tests for Databricks U2M token refresh (Pattern 2)."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.utils import databricks_auth as auth  # noqa: E402

_AWS_WORKSPACE = 'https://adb-1234567890123456.12.azuredatabricks.net'


class TestGetValidDbxToken(unittest.TestCase):
    def setUp(self):
        auth._oidc_endpoint_cache.clear()

    @patch('backend.utils.databricks_auth.db.get_dbx_tokens')
    def test_returns_cached_token_when_not_near_expiry(self, mock_get):
        mock_get.return_value = {
            'workspace_url':  'https://adb-1.2.azuredatabricks.net',
            'cloud_provider': 'azure',
            'access_token':   'valid-access',
            'refresh_token':  'refresh-1',
            'expires_at':     time.time() + 3600,
        }
        record = auth.get_valid_dbx_token('user-1')
        self.assertEqual(record['access_token'], 'valid-access')

    @patch('backend.utils.databricks_auth.db.save_dbx_tokens')
    @patch('backend.utils.databricks_auth.db.get_dbx_tokens')
    @patch('backend.utils.databricks_auth.requests.post')
    @patch.dict(
        'os.environ',
        {
            'DATABRICKS_CLIENT_ID':     'cid',
            'DATABRICKS_CLIENT_SECRET': 'csecret',
        },
    )
    def test_refreshes_expired_token_and_persists_new_refresh_token(
        self, mock_post, mock_get, mock_save,
    ):
        stale = {
            'workspace_url':  _AWS_WORKSPACE,
            'cloud_provider': 'aws',
            'access_token':   'stale-access',
            'refresh_token':  'refresh-old',
            'expires_at':     time.time() - 10,
        }
        refreshed = {
            'workspace_url':  stale['workspace_url'],
            'cloud_provider': 'aws',
            'access_token':   'new-access',
            'refresh_token':  'refresh-new',
            'expires_at':     time.time() + 3600,
        }
        mock_get.side_effect = [stale, stale, refreshed]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'access_token':  'new-access',
            'refresh_token': 'refresh-new',
            'expires_in':    3600,
        }
        mock_post.return_value = mock_resp

        record = auth.get_valid_dbx_token('user-1')

        self.assertEqual(record['access_token'], 'new-access')
        mock_save.assert_called_once()
        args = mock_save.call_args[0]
        self.assertEqual(args[3], 'refresh-new')

    @patch('backend.utils.databricks_auth.db.get_dbx_tokens')
    def test_stale_token_without_refresh_raises(self, mock_get):
        mock_get.return_value = {
            'workspace_url':  'https://adb-1.2.azuredatabricks.net',
            'cloud_provider': 'azure',
            'access_token':   'stale-access',
            'refresh_token':  None,
            'expires_at':     time.time() - 10,
        }
        with self.assertRaises(RuntimeError) as ctx:
            auth.get_valid_dbx_token('user-1')
        self.assertIn('refresh token missing', str(ctx.exception).lower())

    @patch('backend.utils.databricks_auth.db.get_dbx_tokens')
    @patch('backend.utils.databricks_auth.requests.post')
    @patch.dict(
        'os.environ',
        {
            'DATABRICKS_CLIENT_ID':     'cid',
            'DATABRICKS_CLIENT_SECRET': 'csecret',
        },
    )
    def test_refresh_403_raises_reauth_error(self, mock_post, mock_get):
        mock_get.return_value = {
            'workspace_url':  'https://adb-1.2.azuredatabricks.net',
            'cloud_provider': 'azure',
            'access_token':   'stale-access',
            'refresh_token':  'refresh-old',
            'expires_at':     time.time() - 10,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = 'invalid_grant'
        mock_post.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            auth.get_valid_dbx_token('user-1')
        self.assertIn('re-authenticate', str(ctx.exception).lower())


if __name__ == '__main__':
    unittest.main()
