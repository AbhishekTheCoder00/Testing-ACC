"""Unit tests for ACC U2M token refresh."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.clients import acc_client  # noqa: E402


class TestGetValidAccToken(unittest.TestCase):
    @patch('backend.clients.acc.auth_client.db.update_acc_access_token')
    @patch('backend.clients.acc.auth_client.db.get_acc_tokens')
    @patch('backend.clients.acc.auth_client.requests.post')
    @patch.dict(
        'os.environ',
        {'APS_CLIENT_ID': 'cid', 'APS_CLIENT_SECRET': 'csecret'},
    )
    def test_refresh_persists_rotated_refresh_token(
        self, mock_post, mock_get, mock_update,
    ):
        mock_get.return_value = {
            'access_token':  'stale-access',
            'refresh_token': 'refresh-old',
            'expires_at':    time.time() - 10,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'access_token':  'new-access',
            'refresh_token': 'refresh-new',
            'expires_in':    3600,
        }
        mock_post.return_value = mock_resp

        token = acc_client.get_valid_token('user-1')

        self.assertEqual(token, 'new-access')
        mock_update.assert_called_once()
        kwargs = mock_update.call_args.kwargs
        self.assertEqual(kwargs['refresh_token'], 'refresh-new')


if __name__ == '__main__':
    unittest.main()
