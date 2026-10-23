from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from douyin import account_feed  # noqa: E402


SEC_UID = "MS4wLjABAAAAsynthetic_account_01"


def logged_in():
    return {"success": True, "logged_in": True, "risk_page": False}


class AccountFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = ROOT / "fixtures" / "account_feed" / "v1" / "contracts.json"
        cls.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_resolve_user_requires_one_exact_sec_uid(self):
        page = mock.Mock()
        page.evaluate.return_value = {
            "title": "用户搜索",
            "candidates": self.fixture["resolved"]["candidates"],
        }
        with mock.patch.object(account_feed, "settle_login_state", return_value=logged_in()):
            result = account_feed.resolve_user(page, self.fixture["resolved"]["nickname"])
        self.assertTrue(result["success"])
        self.assertEqual(result["account"]["sec_uid"], SEC_UID)
        self.assertEqual(result["identity_key"], "sec_uid")

    def test_resolve_user_javascript_parses(self):
        page = mock.Mock()
        page.evaluate.return_value = {"title": "用户搜索", "candidates": []}
        with mock.patch.object(account_feed, "settle_login_state", return_value=logged_in()):
            account_feed.resolve_user(page, "Synthetic Teacher")
        expression = page.evaluate.call_args.args[0]
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", "new Function(process.argv[1])", expression],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_resolve_user_fails_closed_for_same_name_ambiguity(self):
        page = mock.Mock()
        page.evaluate.return_value = {
            "title": "用户搜索",
            "candidates": self.fixture["ambiguous"]["candidates"],
        }
        with mock.patch.object(account_feed, "settle_login_state", return_value=logged_in()):
            result = account_feed.resolve_user(page, self.fixture["ambiguous"]["nickname"])
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "AMBIGUOUS")
        self.assertEqual(result["exact_match_count"], 2)

    def test_list_posts_returns_server_cursor_and_normalized_ids(self):
        page = mock.Mock()
        page.evaluate.return_value = {
            "http_status": 200,
            "payload": {
                "status_code": 0,
                "has_more": 1,
                "max_cursor": 100,
                "aweme_list": [
                    {
                        "aweme_id": "1000000000000000001",
                        "desc": "Synthetic tutorial",
                        "author": {"sec_uid": SEC_UID, "nickname": "Old Name"},
                        "video": {"duration": 91000},
                        "is_top": 1,
                    }
                ],
            },
        }
        with mock.patch.object(account_feed, "settle_login_state", return_value=logged_in()):
            result = account_feed.list_user_posts(page, SEC_UID)
        self.assertTrue(result["success"])
        self.assertEqual(result["next_cursor"], "100")
        self.assertEqual(result["cursor_source"], "douyin_aweme_post_response.max_cursor")
        self.assertEqual(result["videos"][0]["video_id"], "1000000000000000001")
        self.assertTrue(result["videos"][0]["is_top"])

    def test_list_posts_rejects_non_advancing_cursor(self):
        page = mock.Mock()
        page.evaluate.return_value = {
            "http_status": 200,
            "payload": {"status_code": 0, "has_more": 1, "max_cursor": 100, "aweme_list": []},
        }
        with mock.patch.object(account_feed, "settle_login_state", return_value=logged_in()):
            result = account_feed.list_user_posts(page, SEC_UID, cursor="100")
        self.assertFalse(result["success"])
        self.assertEqual(result["state"], "CURSOR_INVALID")

    def test_login_expiry_stops_before_api_fetch(self):
        page = mock.Mock()
        state = {"success": True, "logged_in": False, "risk_page": False}
        with mock.patch.object(account_feed, "settle_login_state", return_value=state):
            result = account_feed.list_user_posts(page, SEC_UID)
        self.assertEqual(result["state"], "AUTH_REQUIRED")
        page.evaluate.assert_not_called()

    def test_risk_page_stops_before_api_fetch(self):
        page = mock.Mock()
        state = {"success": True, "logged_in": False, "risk_page": True}
        with mock.patch.object(account_feed, "settle_login_state", return_value=state):
            result = account_feed.list_user_posts(page, SEC_UID)
        self.assertEqual(result["state"], "RISK_PAGE")
        self.assertTrue(result["needs_user_verification"])
        page.evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()