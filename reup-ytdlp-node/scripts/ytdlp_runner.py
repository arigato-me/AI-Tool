"""Chạy yt-dlp CLI qua subprocess — yt-dlp là 1 package/CLI bên thứ 3 hoàn chỉnh (không phải
script Python tự viết như translate/editor), nên không có 1 hàm Python thuần để import gọi
thẳng. Giữ nguyên đúng cách dùng args-passthrough của `docker_yt-dlp` gốc (mọi arg CLI hiện có
vẫn dùng được y hệt), chỉ đổi cách gọi từ `docker compose run --rm yt-dlp <args>` (đồng bộ,
foreground) sang submit qua job-queue (bất đồng bộ).

Dùng `Popen` + polling `communicate(timeout=...)` theo đúng pattern khuyến nghị của tài liệu
`subprocess` (gọi lại `communicate()` sau `TimeoutExpired` không mất dữ liệu đã đọc) thay vì
`subprocess.run()` (chặn cứng, không có chỗ nào để kiểm tra cờ huỷ giữa chừng) — cho phép
`worker.py` kill tiến trình ngay khi người dùng bấm Huỷ lúc đang tải, thay vì phải đợi tải
xong mới dừng được (xem `queue_lib.request_cancel`)."""
from __future__ import annotations

import subprocess
import time
from typing import Callable

POLL_INTERVAL_S = 1.0
TIMEOUT_S = 3600
FORBIDDEN_RETRIES = 3
FORBIDDEN_RETRY_SLEEP_S = 3.0


class YtdlpCancelled(RuntimeError):
    """Raise khi `should_cancel()` báo true giữa lúc yt-dlp đang tải — worker.py bắt riêng
    exception này để ghi status 'cancelled' thay vì 'failed'."""


def _run_once(args: list[str], cwd: str, should_cancel: Callable[[], bool] | None) -> dict:
    start = time.time()
    proc = subprocess.Popen(
        ["yt-dlp", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout = stderr = ""
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=POLL_INTERVAL_S)
            break
        except subprocess.TimeoutExpired:
            if should_cancel is not None and should_cancel():
                proc.terminate()
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                raise YtdlpCancelled("huỷ theo yêu cầu người dùng lúc đang tải")
            if time.time() - start > TIMEOUT_S:
                proc.kill()
                proc.communicate()
                raise RuntimeError(f"yt-dlp timeout sau {TIMEOUT_S}s")

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "args": args,
        "stdout_tail": "\n".join(stdout.splitlines()[-30:]),
        "stderr_tail": "\n".join(stderr.splitlines()[-30:]),
        "elapsed_s": round(time.time() - start, 2),
    }


def run_ytdlp(args: list[str], cwd: str = "/downloads", should_cancel: Callable[[], bool] | None = None) -> dict:
    if not args:
        raise RuntimeError("thiếu args cho yt-dlp (vd URL, -o, ...)")

    result = None
    for attempt in range(1, FORBIDDEN_RETRIES + 1):
        result = _run_once(args, cwd, should_cancel)
        if result["returncode"] == 0:
            return result
        # HTTP 403 trên link CDN Douyin thường là do link ký (signed URL) hết hạn/transient,
        # không phải lỗi extractor — yt-dlp coi 403 là fatal nên không tự retry. Chạy lại cả
        # tiến trình (không chỉ request) để sinh cookie/a_bogus + link CDN ký mới mỗi lần.
        if "403" not in result["stderr_tail"] or attempt == FORBIDDEN_RETRIES:
            break
        if should_cancel is not None and should_cancel():
            break
        time.sleep(FORBIDDEN_RETRY_SLEEP_S)

    if result["returncode"] != 0:
        raise RuntimeError(result["stderr_tail"] or f"yt-dlp thoát với mã lỗi {result['returncode']}")
    return result
