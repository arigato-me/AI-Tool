"""Unit test thuần stdlib (unittest + unittest.mock) cho node_client.py — không thêm dependency
mới, khớp mức rigor hiện có của repo (xem reup-editor-node/scripts/test_music_library.py). Chạy:
python3 -m unittest test_node_client -v (không cần Redis/GPU, mock thẳng module `requests`).

Trọng tâm: retry cho lỗi mạng thoáng qua KHÔNG được nuốt lỗi thật (job failed/cancelled/4xx vẫn
raise ngay lần đầu), và không lặp vô ích khi node con trả "job không tồn tại"."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

import node_client as nc


def _resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(f"{status_code}") if status_code >= 400 else None
    )
    return r


class SubmitJobTests(unittest.TestCase):
    @patch("node_client.requests.post")
    def test_success_first_try(self, mock_post):
        mock_post.return_value = _resp(200, {"ok": True, "job_id": "abc"})
        self.assertEqual(nc.submit_job("http://x", {}), "abc")
        self.assertEqual(mock_post.call_count, 1)

    @patch("node_client.time.sleep", return_value=None)
    @patch("node_client.requests.post")
    def test_retries_on_connection_error_then_succeeds(self, mock_post, _sleep):
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("restart"),
            _resp(200, {"ok": True, "job_id": "abc"}),
        ]
        self.assertEqual(nc.submit_job("http://x", {}), "abc")
        self.assertEqual(mock_post.call_count, 2)

    @patch("node_client.time.sleep", return_value=None)
    @patch("node_client.requests.post")
    def test_exhausts_retries_raises_node_job_error(self, mock_post, _sleep):
        mock_post.side_effect = requests.exceptions.ConnectionError("down")
        with self.assertRaises(nc.NodeJobError):
            nc.submit_job("http://x", {})
        self.assertEqual(mock_post.call_count, nc._POST_RETRIES)

    @patch("node_client.requests.post")
    def test_job_rejected_ok_false_raises_immediately_no_retry(self, mock_post):
        mock_post.return_value = _resp(200, {"ok": False, "error": "cmd không hợp lệ"})
        with self.assertRaises(nc.NodeJobError):
            nc.submit_job("http://x", {})
        self.assertEqual(mock_post.call_count, 1)


class WaitForJobTests(unittest.TestCase):
    @patch("node_client.requests.get")
    def test_finished_returns_result(self, mock_get):
        mock_get.return_value = _resp(200, {"ok": True, "status": "finished", "result": {"x": 1}})
        self.assertEqual(nc.wait_for_job("http://x", "j1"), {"x": 1})

    @patch("node_client.requests.get")
    def test_failed_raises_node_job_error(self, mock_get):
        mock_get.return_value = _resp(200, {"ok": True, "status": "failed", "error": "boom"})
        with self.assertRaises(nc.NodeJobError):
            nc.wait_for_job("http://x", "j1")

    @patch("node_client.requests.get")
    def test_cancelled_raises_node_cancelled(self, mock_get):
        mock_get.return_value = _resp(200, {"ok": True, "status": "cancelled"})
        with self.assertRaises(nc.NodeCancelled):
            nc.wait_for_job("http://x", "j1")

    @patch("node_client.requests.get")
    def test_job_not_found_raises_immediately_not_timeout(self, mock_get):
        mock_get.return_value = _resp(200, {"ok": False, "error": "job không tồn tại hoặc đã hết hạn"})
        with self.assertRaises(nc.NodeJobError) as ctx:
            nc.wait_for_job("http://x", "j1", timeout=999)
        self.assertIn("không tồn tại", str(ctx.exception))
        self.assertEqual(mock_get.call_count, 1)

    @patch("node_client.time.sleep", return_value=None)
    @patch("node_client.requests.get")
    def test_transient_network_error_retried_then_finishes(self, mock_get, _sleep):
        mock_get.side_effect = [
            requests.exceptions.ConnectionError("restart"),
            requests.exceptions.Timeout("slow"),
            _resp(200, {"ok": True, "status": "finished", "result": {"x": 2}}),
        ]
        self.assertEqual(nc.wait_for_job("http://x", "j1"), {"x": 2})
        self.assertEqual(mock_get.call_count, 3)

    @patch("node_client.time.sleep", return_value=None)
    @patch("node_client.requests.get")
    def test_5xx_retried_then_finishes(self, mock_get, _sleep):
        mock_get.side_effect = [_resp(503), _resp(200, {"ok": True, "status": "finished", "result": {}})]
        nc.wait_for_job("http://x", "j1")
        self.assertEqual(mock_get.call_count, 2)

    @patch("node_client.requests.get")
    def test_still_pending_at_deadline_raises_timeout_not_hang(self, mock_get):
        mock_get.return_value = _resp(200, {"ok": True, "status": "started"})
        with self.assertRaises(nc.NodeJobError) as ctx:
            nc.wait_for_job("http://x", "j1", poll_interval=0.01, timeout=0.05)
        self.assertIn("timeout", str(ctx.exception))


class SubmitAndWaitTests(unittest.TestCase):
    @patch("node_client.requests.get")
    @patch("node_client.requests.post")
    def test_on_submitted_called_before_poll_with_job_id(self, mock_post, mock_get):
        mock_post.return_value = _resp(200, {"ok": True, "job_id": "j1"})
        mock_get.return_value = _resp(200, {"ok": True, "status": "finished", "result": {"x": 3}})
        seen = []
        result = nc.submit_and_wait("http://x", {}, on_submitted=seen.append)
        self.assertEqual(seen, ["j1"])
        self.assertEqual(result, {"x": 3})


if __name__ == "__main__":
    unittest.main()
