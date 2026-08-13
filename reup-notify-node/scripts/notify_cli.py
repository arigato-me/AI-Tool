"""Dispatcher run_notify() (gọi từ worker.py) + subcommand CLI `browser-login` (bootstrap
session lần đầu, xem browser_adapter.py::interactive_login). Mỗi platform bọc try/except
riêng — 1 platform lỗi (chưa đăng nhập, hết session, sai config...) không kéo cả job fail,
job vẫn `mark_done` với kết quả partial-failure để caller tự soi từng target."""
from __future__ import annotations

import argparse
import sys

import browser_adapter
import telegram_adapter
import webhook_adapter


def run_notify(
    platforms: list[str],
    message: str,
    file_path: str | None = None,
    chat_id: str | None = None,
    pipeline_id: str | None = None,
    video_name: str | None = None,
) -> dict:
    results: dict[str, dict] = {}
    for platform in platforms:
        base, _, arg = platform.partition(":")
        try:
            if base == "telegram":
                results[platform] = telegram_adapter.send(message, file_path, chat_id=chat_id)
            elif base == "webhook":
                results[platform] = webhook_adapter.send(arg, message, file_path)
            elif base in browser_adapter.PLATFORM_CONFIGS:
                results[platform] = browser_adapter.send(base, message, file_path)
            else:
                results[platform] = {"ok": False, "error": f"platform không hỗ trợ: {platform!r}"}
        except Exception as e:
            results[platform] = {"ok": False, "error": str(e)}

    sent = sum(1 for r in results.values() if r.get("ok"))
    return {"ok": sent > 0, "sent": sent, "total": len(platforms), "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    login_p = sub.add_parser("browser-login", help="bootstrap session 1 lần cho whatsapp/zalo/messenger")
    login_p.add_argument("platform", choices=list(browser_adapter.PLATFORM_CONFIGS))
    login_p.add_argument("--timeout-s", type=int, default=300)

    args = parser.parse_args()
    if args.cmd == "browser-login":
        browser_adapter.interactive_login(args.platform, timeout_s=args.timeout_s)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"lỗi: {e}", file=sys.stderr)
        sys.exit(1)
