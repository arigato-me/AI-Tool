"""1 test chạy được cho logic dispatch/partial-failure của run_notify() — không cần Redis,
Playwright, hay credential thật (mock thẳng 3 adapter). `python3 -m pytest test_notify_cli.py`
từ thư mục scripts/, hoặc `python3 test_notify_cli.py` (dùng assert thuần, không cần pytest)."""
from __future__ import annotations

from unittest.mock import patch

from notify_cli import run_notify


def test_all_succeed():
    with patch("notify_cli.telegram_adapter.send", return_value={"ok": True}):
        result = run_notify(["telegram"], "hi")
    assert result == {"ok": True, "sent": 1, "total": 1, "results": {"telegram": {"ok": True}}}


def test_partial_failure_does_not_raise():
    with (
        patch("notify_cli.telegram_adapter.send", return_value={"ok": True}),
        patch("notify_cli.browser_adapter.send", side_effect=RuntimeError("chưa đăng nhập whatsapp")),
    ):
        result = run_notify(["telegram", "whatsapp"], "hi")
    assert result["ok"] is True  # ít nhất 1 platform gửi được -> job vẫn coi là ok tổng thể
    assert result["sent"] == 1
    assert result["total"] == 2
    assert result["results"]["telegram"] == {"ok": True}
    assert result["results"]["whatsapp"]["ok"] is False
    assert "chưa đăng nhập" in result["results"]["whatsapp"]["error"]


def test_all_fail():
    with patch("notify_cli.telegram_adapter.send", side_effect=RuntimeError("thiếu bot_token")):
        result = run_notify(["telegram"], "hi")
    assert result["ok"] is False
    assert result["sent"] == 0


def test_webhook_dispatch_passes_name():
    with patch("notify_cli.webhook_adapter.send", return_value={"ok": True}) as m:
        run_notify(["webhook:myapp"], "hi", file_path="/source/f.mp4")
    m.assert_called_once_with("myapp", "hi", "/source/f.mp4")


def test_unknown_platform():
    result = run_notify(["not-a-platform"], "hi")
    assert result["results"]["not-a-platform"]["ok"] is False


if __name__ == "__main__":
    test_all_succeed()
    test_partial_failure_does_not_raise()
    test_all_fail()
    test_webhook_dispatch_passes_name()
    test_unknown_platform()
    print("all assertions passed")
