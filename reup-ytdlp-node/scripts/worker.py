from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import redis as redis_lib

import queue_lib as q
from ytdlp_runner import YtdlpCancelled, run_ytdlp

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "ytdlp-worker"


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    """Thread nền riêng — job xử lý xong mới quay lại đầu `while True` gọi heartbeat, nên
    job chạy lâu hơn HEARTBEAT_TTL_S (30s) làm key hết hạn giữa chừng dù worker vẫn đang bận,
    khiến `/health`/`/nodes/status` tưởng nhầm là down. Bug thật gặp: dashboard monitor báo
    đúng node đang xử lý job là down."""
    while True:
        try:
            q.heartbeat(conn, ROLE)
        except Exception as e:
            # Bug thật gặp: 1 lỗi Redis (network blip/idle-timeout, không cần Redis chết hẳn)
            # ném exception ra khỏi vòng lặp làm CHẾT LUÔN thread này — không ai khởi động lại,
            # nên node báo down vĩnh viễn dù main loop vẫn xử lý job bình thường, tới khi có
            # người restart tay. Nuốt lỗi + lặp tiếp: redis-py tự mở lại kết nối ở lần gọi kế,
            # không cần tự viết logic reconnect.
            print(f"[heartbeat] lỗi ghi (bỏ qua, thử lại sau {interval}s): {e}", file=sys.stderr)
        time.sleep(interval)


def _log_retention_loop(interval: float = 3600.0) -> None:
    """Thread nền dọn `/logs/*.jsonl` cũ hơn `LOG_RETENTION_DAYS` (mặc định 2 ngày) — chạy
    độc lập với xử lý job, không phụ thuộc `EVENT_LOG_ENABLED` (dọn nốt log cũ nếu có)."""
    while True:
        q.prune_old_logs()
        time.sleep(interval)


def main() -> None:
    conn = redis_lib.Redis.from_url(REDIS_URL)
    threading.Thread(target=_heartbeat_loop, args=(conn,), daemon=True).start()
    threading.Thread(target=_log_retention_loop, daemon=True).start()

    recovered = q.recover_stale_jobs(conn)
    if recovered:
        print(f"[worker] Khôi phục {recovered} job bị kẹt từ lần chạy trước", file=sys.stderr)

    print(f"[worker] Sẵn sàng nhận job (service={q.SERVICE})", file=sys.stderr)
    while True:
        job_id = q.pop_job(conn, timeout=5)
        if job_id is None:
            continue

        job = q.get_job(conn, job_id)
        if job is None:
            continue
        payload = job["payload"]
        pipeline_id = payload.get("pipeline_id")
        video_name = payload.get("video_name")

        # Cờ huỷ có thể đã được đặt TRƯỚC KHI worker rảnh tay tới lượt job này (job còn nằm
        # ở 'pending' lúc người dùng bấm Huỷ) — kiểm ngay, khỏi tốn công spawn yt-dlp rồi mới
        # huỷ. Không cần kiểm should_cancel giữa chừng nữa cho case này vì subprocess chưa
        # chạy phút nào.
        if pipeline_id and q.is_cancel_requested(conn, pipeline_id):
            q.mark_cancelled(conn, job_id)
            q.clear_cancel(conn, pipeline_id)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_cancelled"})
            print(f"[worker] job {job_id} bị huỷ trước khi chạy", file=sys.stderr)
            continue

        q.mark_started(conn, job_id)
        q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                     "role": ROLE, "event": "job_start"})
        t0 = time.time()

        def _should_cancel(pid: str | None = pipeline_id) -> bool:
            return bool(pid) and q.is_cancel_requested(conn, pid)

        # Rate-limit theo domain nguồn (vd douyin.com) — TÁCH RIÊNG khỏi PIPELINE_CONCURRENCY
        # của orchestrator, xem docstring đầy đủ ở rate_limit_wait() trong queue_lib.py và plan
        # lưu ở .../warm-knitting-map.md. args luôn có ít nhất 1 phần tử là URL (orchestrator
        # luôn append URL cuối cùng, nhưng quét cả list cho chắc — gọi trực tiếp qua API/CLI có
        # thể thứ tự khác) — không tìm thấy URL nào thì bỏ qua, không rate-limit gì cả.
        url = next((a for a in payload["args"] if a.startswith(("http://", "https://"))), None)
        if url:
            q.rate_limit_wait(conn, url)

        try:
            result = run_ytdlp(payload["args"], should_cancel=_should_cancel)
            q.mark_done(conn, job_id, result)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_done", "elapsed_s": round(time.time() - t0, 1),
                         "result": result})
            print(f"[worker] job {job_id} xong sau {time.time() - t0:.1f}s", file=sys.stderr)
        except YtdlpCancelled:
            q.mark_cancelled(conn, job_id)
            if pipeline_id:
                q.clear_cancel(conn, pipeline_id)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_cancelled", "elapsed_s": round(time.time() - t0, 1)})
            print(f"[worker] job {job_id} bị huỷ giữa lúc tải sau {time.time() - t0:.1f}s", file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            err_text = traceback.format_exc()[-4000:]
            q.mark_failed(conn, job_id, err_text)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_failed", "elapsed_s": round(time.time() - t0, 1),
                         "error": err_text})
            print(f"[worker] job {job_id} lỗi: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
